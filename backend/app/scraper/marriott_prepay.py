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
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperInterruptedError
from backend.app.scraper.marriott import (
    RATE_CARD_RE,
    SessionRotator,
    _build_rates_url,
    _profile_dir,
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

# Once the hotel list is known, checks run across several concurrent browser
# sessions instead of one at a time -- each worker gets its own dedicated
# slice of profile directories (never shared with another worker, since two
# patchright contexts can't hold the same profile dir open at once) and
# shuffles its own rotation order (`randomize=True` on SessionRotator), so
# a block in one session doesn't correlate with which profile the next
# worker picks. Workers still throttle between their own checks (see module
# docstring) -- this trades total wall-clock time for more concurrent
# "presence" against the site, not for skipping the per-check delay.
PREPAY_WORKER_COUNT = 3
PROFILES_PER_WORKER = 5


def _merge_codes(
    known: list[tuple[str, str]], new: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Combine already-cached (code, name) pairs with freshly fetched ones,
    deduping by code."""
    seen = {code for code, _ in known}
    merged = list(known)
    for code, name in new:
        if code not in seen:
            seen.add(code)
            merged.append((code, name))
    return merged


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
    except PatchrightTimeoutError as exc:
        # A block can also show up as this click never finding its target
        # (an Access Denied page has no "View Rates" button) -- without this
        # check that silently looked identical to "no rate button rendered",
        # permanently recording a blocked check as "no prepay available".
        title = await page.title()
        if "access denied" in title.lower():
            raise ScraperBlockedError(f"Marriott blocked the request for {url}") from exc
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


async def _prepay_worker(
    playwright,
    profile_dirs: list[str],
    queue: "asyncio.Queue[tuple[str, str]]",
    req: SearchRequest,
    nights: int,
    checked_codes: set[str],
    results: list[Hotel],
) -> None:
    """Pull (code, name) pairs off the shared queue and check each for a
    prepay rate, in its own dedicated browser session. Runs until the queue
    is empty. If this worker's browser is closed/crashes, it just stops --
    other workers keep going, and everything checked so far is already
    saved (see the incremental prepay_store.save below).
    """
    async with SessionRotator(playwright, profile_dirs=profile_dirs, randomize=True) as session:
        first = True
        try:
            while True:
                try:
                    code, name = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                if not first:
                    await asyncio.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
                first = False

                async def check(page, code=code, name=name):
                    return await _check_hotel_prepay(page, req, code, name, nights)

                hotel = await session.run(check)
                checked_codes.add(code)
                if hotel is not None:
                    results.append(hotel)
                prepay_store.save(req, checked_codes, results)
        except ScraperInterruptedError:
            return


async def search_prepay(req: SearchRequest, limit: int | None = None) -> list[Hotel]:
    """Return hotels (from a Marriott search) that offer a Prepay Non-refundable rate.

    The full hotel list for this query is fetched by walking every results
    page (see marriott.list_all_hotel_codes) and cached in
    hotel_list_store.py, including how many pages have been walked so far.
    A later call for the same query -- e.g. after the browser was closed
    manually mid-pagination -- skips straight past those pages instead of
    re-walking them from page 1. Once the listing is marked complete,
    later calls skip pagination entirely and use the cached list.

    Once the hotel list is known, per-hotel prepay checks run across
    PREPAY_WORKER_COUNT concurrent browser sessions (see module docstring
    on PREPAY_WORKER_COUNT) pulling from a shared queue, each still
    throttled between its own checks. Which hotels have been checked and
    their results persist to a local JSON file (prepay_store.py) keyed by
    location/dates/guest count: hotels already checked in a prior call with
    the same query are skipped, and `limit` caps how many *new*
    (not-yet-checked) hotels this call checks -- so calling this repeatedly
    with the same query and a small limit continues the scan from where the
    last call left off, rather than re-checking from the start. Pass
    limit=None to check all remaining unchecked hotels in one call. The
    returned list is the full accumulated result set across all calls for
    this query, not just this call's batch.

    Auto-recovers from blocks (see marriott.SessionRotator): a block during
    listing or a per-hotel check rotates that session to a fresh browser
    profile and retries, rather than failing the whole call on the first
    block.

    If a browser window is closed manually (or crashes) partway through,
    the affected session stops (other concurrent sessions keep going)
    instead of raising -- both the listing progress and the per-hotel
    results are saved incrementally as they happen (not just at the end),
    so nothing already discovered is lost either way.

    Raises:
        ScraperBlockedError: a session is still blocked after exhausting
            its own profile pool.
    """
    nights = (req.check_out - req.check_in).days

    checked_codes, results = prepay_store.load(req)
    known_codes, pages_fetched, complete = hotel_list_store.load(req)

    async with async_playwright() as playwright:
        if not complete:
            pages_done_this_call = 0

            def on_progress(codes_so_far):
                nonlocal pages_done_this_call
                pages_done_this_call += 1
                combined = _merge_codes(known_codes, codes_so_far)
                hotel_list_store.save(
                    req, combined, pages_fetched + pages_done_this_call, complete=False
                )

            async with SessionRotator(playwright) as listing_session:
                try:
                    new_codes = await listing_session.run(
                        lambda page: list_all_hotel_codes(
                            page, req, on_progress=on_progress, skip_pages=pages_fetched
                        )
                    )
                    hotel_codes = _merge_codes(known_codes, new_codes)
                    hotel_list_store.save(
                        req, hotel_codes, pages_fetched + pages_done_this_call, complete=True
                    )
                except ScraperInterruptedError:
                    return results
        else:
            hotel_codes = known_codes

        remaining = [(code, name) for code, name in hotel_codes if code not in checked_codes]
        batch = remaining[:limit] if limit is not None else remaining
        if not batch:
            return results

        queue: asyncio.Queue = asyncio.Queue()
        for item in batch:
            queue.put_nowait(item)

        total_profiles = PREPAY_WORKER_COUNT * PROFILES_PER_WORKER
        all_profile_dirs = [_profile_dir(i) for i in range(total_profiles)]
        worker_profile_chunks = [
            all_profile_dirs[i * PROFILES_PER_WORKER : (i + 1) * PROFILES_PER_WORKER]
            for i in range(PREPAY_WORKER_COUNT)
        ]

        await asyncio.gather(
            *[
                _prepay_worker(playwright, chunk, queue, req, nights, checked_codes, results)
                for chunk in worker_profile_chunks
            ]
        )

        return results
