"""Prepay-rate checker for hotels already found by marriott.search().

For each given hotel, opens that hotel's rate page and checks whether its
first room type offers a "Prepay Non-refundable" rate plan, capturing that
plan's tax-inclusive Member Rate price. Annotates each hotel's
`supports_prepay` in place rather than filtering the list -- the caller
decides what to do with hotels that don't support it.

THROTTLING (read before changing timing here): a back-to-back scan of ~40
hotels at roughly 3s apart triggered an Akamai block (403 "Access Denied")
partway through on 2026-08-27 -- the same profile that had just done a
normal single search moments before was blocked even on a fresh plain
request afterward, suggesting a session/IP-level penalty, not just a
per-page check. There is no known safe rate. This module waits a
randomized DELAY_MIN_SECONDS-DELAY_MAX_SECONDS between each check a single
worker makes; if this still gets blocked, increase the delay further
rather than adding more workers.
"""

import asyncio
import random

from patchright.async_api import TimeoutError as PatchrightTimeoutError
from patchright.async_api import Page, async_playwright

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import prepay_store
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperInterruptedError
from backend.app.scraper.marriott import RATE_CARD_RE, SessionRotator, _build_rates_url, _profile_dir

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

# Default cap on how many new hotels a single check_prepay() call checks --
# callers that want more (e.g. the frontend's "check more" button) pass an
# explicit larger limit.
DEFAULT_PREPAY_LIMIT = 10


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
        code=code,
        supports_prepay=True,
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


async def check_prepay(req: SearchRequest, hotels: list[Hotel], limit: int | None = None) -> list[Hotel]:
    """Check each of the given hotels (from a completed marriott.search())
    for a Prepay Non-refundable rate, setting `hotel.supports_prepay` to
    True/False for every hotel checked (this call or a prior one for the
    same query) and leaving it None for any not yet checked.

    Which hotels have been checked and their results persist to a local
    JSON file (prepay_store.py) keyed by location/dates/guest count:
    hotels already checked in a prior call with the same query are
    skipped, and `limit` caps how many *new* (not-yet-checked) hotels this
    call checks -- so calling this repeatedly with the same query and a
    small limit continues the scan from where the last call left off.
    Pass limit=None to check all remaining unchecked hotels in one call.

    Checks run across PREPAY_WORKER_COUNT concurrent browser sessions (see
    module docstring), each still throttled between its own checks.
    Auto-recovers from blocks (see marriott.SessionRotator): a block in one
    session rotates that session to a fresh browser profile and retries,
    rather than failing the whole call on the first block.

    If a browser window is closed manually (or crashes) partway through,
    the affected session just stops (other concurrent sessions keep going)
    instead of raising -- results are saved incrementally as they happen,
    so nothing already discovered is lost either way.
    """
    nights = (req.check_out - req.check_in).days
    checked_codes, results = prepay_store.load(req)

    candidates = [h for h in hotels if h.code and h.code not in checked_codes]
    batch = candidates[:limit] if limit is not None else candidates

    if batch:
        queue: asyncio.Queue = asyncio.Queue()
        for hotel in batch:
            queue.put_nowait((hotel.code, hotel.name))

        total_profiles = PREPAY_WORKER_COUNT * PROFILES_PER_WORKER
        all_profile_dirs = [_profile_dir(i) for i in range(total_profiles)]
        worker_profile_chunks = [
            all_profile_dirs[i * PROFILES_PER_WORKER : (i + 1) * PROFILES_PER_WORKER]
            for i in range(PREPAY_WORKER_COUNT)
        ]

        async with async_playwright() as playwright:
            await asyncio.gather(
                *[
                    _prepay_worker(playwright, chunk, queue, req, nights, checked_codes, results)
                    for chunk in worker_profile_chunks
                ]
            )

        checked_codes, results = prepay_store.load(req)

    supported_codes = {h.code for h in results if h.code}
    for hotel in hotels:
        if hotel.code in checked_codes:
            hotel.supports_prepay = hotel.code in supported_codes

    return hotels
