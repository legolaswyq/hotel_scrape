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
from backend.app.scraper.exceptions import ScraperBlockedError
from backend.app.scraper.marriott import (
    _PROFILE_DIR,
    PROPERTY_CARD_SELECTOR,
    RATE_CARD_RE,
    RESULTS_TIMEOUT_MS,
    _build_rates_url,
    _build_search_url,
    _extract_hotel_codes,
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
    """Return only hotels (from a Marriott search) that offer a Prepay Non-refundable rate.

    Slow by design (see module docstring): one hotel rate page at a time,
    with a randomized delay between each, to avoid the block a fast burst
    of requests triggered previously. `limit` caps how many of the search
    result hotels are checked, in listed order -- useful for trying a
    handful before committing to a full scan.

    Raises:
        ScraperBlockedError: the site returned a 403 or an Akamai block page.
    """
    nights = (req.check_out - req.check_in).days
    search_url = _build_search_url(req)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            _PROFILE_DIR,
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            response = await page.goto(search_url, wait_until="domcontentloaded")
            if response is not None and response.status == 403:
                raise ScraperBlockedError(f"Marriott returned 403 for {search_url}")
            await page.wait_for_selector(PROPERTY_CARD_SELECTOR, timeout=RESULTS_TIMEOUT_MS)

            hotel_codes = _extract_hotel_codes(await page.content())
            if limit is not None:
                hotel_codes = hotel_codes[:limit]

            results: list[Hotel] = []
            for i, (code, name) in enumerate(hotel_codes):
                if i > 0:
                    await asyncio.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
                hotel = await _check_hotel_prepay(page, req, code, name, nights)
                if hotel is not None:
                    results.append(hotel)

            return results
        finally:
            await context.close()
