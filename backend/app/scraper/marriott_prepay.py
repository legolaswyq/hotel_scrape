"""Prepay-rate scanner for marriott.com search results.

For each hotel in a search's results, opens that hotel's rate page and
checks whether its first room type offers a "Prepay Non-refundable" rate
plan, capturing that plan's tax-inclusive Member Rate price. Only hotels
with a prepay option are returned.

THROTTLING (read before changing timing here): a back-to-back scan of ~40
hotels at roughly 3s apart triggered an Akamai block (403 "Access Denied")
partway through on 2026-08-27 -- the same profile that had just done a
normal single search moments before was blocked even on a fresh plain
request afterward, suggesting a session/IP-level penalty, not just a
per-page check. There is no known safe rate. This module waits a
randomized DELAY_MIN_SECONDS-DELAY_MAX_SECONDS between hotels; if this
still gets blocked, increase the delay further rather than parallelizing
or retrying immediately.
"""

import asyncio
import random

from patchright.async_api import TimeoutError as PatchrightTimeoutError
from patchright.async_api import Page, async_playwright

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import hotel_list_store, prepay_store
from backend.app.scraper.exceptions import ScraperBlockedError
from backend.app.scraper.marriott import (
    _PROFILE_DIR,
    RATE_CARD_RE,
    _build_rates_url,
    list_all_hotel_codes,
)

PREPAY_MARKER = "Prepay Non-refundable"
PREPAY_CHUNK_SIZE = 6000
RATE_LOAD_TIMEOUT_MS = 20_000
RATE_PAGE_HYDRATION_WAIT_MS = 3_000
VIEW_RATES_CLICK_TIMEOUT_MS = 10_000
VIEW_RATES_RENDER_WAIT_MS = 2_500

DELAY_MIN_SECONDS = 6.0
DELAY_MAX_SECONDS = 12.0


def _extract_prepay_member_price(page_html: str) -> float | None:
    """Tax-inclusive Member Rate price for the Prepay Non-refundable plan, if present."""
    idx = page_html.find(PREPAY_MARKER)
    if idx == -1:
        return None
    chunk = page_html[idx : idx + PREPAY_CHUNK_SIZE]
    for rate_name, price_a, price_b in RATE_CARD_RE.findall(chunk):
        if rate_name.strip() == "Member Rate":
            try:
                return max(float(price_a.replace(",", "")), float(price_b.replace(",", "")))
            except ValueError:
                return None
    return None


async def _check_hotel_prepay(page: Page, req: SearchRequest, code: str, name: str, nights: int) -> Hotel | None:
    url = _build_rates_url(req, code)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=RATE_LOAD_TIMEOUT_MS)
    if response is not None and response.status == 403:
        raise ScraperBlockedError(f"Marriott returned 403 for {url}")

    # The "View Rates" button exists in the DOM right after domcontentloaded
    # but its click handler isn't wired up until the page's JS framework
    # finishes hydrating -- clicking immediately is a silent no-op. Confirmed
    # by reproducing with/without this wait against the live site.
    await page.wait_for_timeout(RATE_PAGE_HYDRATION_WAIT_MS)

    try:
        await page.get_by_role("button", name="View Rates").first.click(
            timeout=VIEW_RATES_CLICK_TIMEOUT_MS
        )
    except PatchrightTimeoutError:
        return None

    await page.wait_for_timeout(VIEW_RATES_RENDER_WAIT_MS)
    price = _extract_prepay_member_price(await page.content())
    if price is None:
        return None

    return Hotel(
        name=name,
        price_per_night=price,
        total_price=price * nights,
        currency="USD",
        url=url,
    )


async def search_prepay(req: SearchRequest, limit: int | None = None) -> list[Hotel]:
    """Return hotels (from a Marriott search) that offer a Prepay Non-refundable rate.

    Slow by design (see module docstring): one hotel rate page at a time,
    with a randomized delay between each, to avoid the block a fast burst
    of requests triggered previously.

    The full hotel list for this query is fetched once (walking every
    results page, see marriott.list_all_hotel_codes) and cached in
    hotel_list_store.py; later calls for the same query reuse the cached
    list instead of re-paginating.

    Which hotels have been checked and their results persist to a local
    JSON file (prepay_store.py) keyed by location/dates/guest count:
    hotels already checked in a prior call with the same query are
    skipped, and `limit` caps how many *new* (not-yet-checked) hotels this
    call checks -- so calling this repeatedly with the same query and a
    small limit continues the scan from where the last call left off,
    rather than re-checking from the start. Pass limit=None to check all
    remaining unchecked hotels in one call. The returned list is the full
    accumulated result set across all calls for this query, not just this
    call's batch.

    Raises:
        ScraperBlockedError: the site returned a 403 or an Akamai block page.
    """
    nights = (req.check_out - req.check_in).days

    checked_codes, results = prepay_store.load(req)
    hotel_codes = hotel_list_store.load(req)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            _PROFILE_DIR,
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            if hotel_codes is None:
                hotel_codes = await list_all_hotel_codes(page, req)
                hotel_list_store.save(req, hotel_codes)

            remaining = [(code, name) for code, name in hotel_codes if code not in checked_codes]
            batch = remaining[:limit] if limit is not None else remaining

            for i, (code, name) in enumerate(batch):
                if i > 0:
                    await asyncio.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
                hotel = await _check_hotel_prepay(page, req, code, name, nights)
                checked_codes.add(code)
                if hotel is not None:
                    results.append(hotel)
                prepay_store.save(req, checked_codes, results)

            return results
        finally:
            await context.close()
