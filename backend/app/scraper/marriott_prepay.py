"""Prepay-rate checker for hotels already found by marriott.search().

For each given hotel, opens that hotel's rate page and checks whether any
room type offers a "Prepay Non-refundable" rate plan, capturing that plan's
tax-inclusive Member Rate price and which room type it's for. Annotates
each hotel's `supports_prepay` (and `room_type`) in place rather than
filtering the list -- the caller decides what to do with hotels that don't
support it.

THROTTLING (read before changing timing here): a back-to-back scan of ~40
hotels at roughly 3s apart triggered an Akamai block (403 "Access Denied")
partway through on 2026-08-27 -- the same profile that had just done a
normal single search moments before was blocked even on a fresh plain
request afterward, suggesting a session/IP-level penalty, not just a
per-page check. There is no known safe rate. This module waits a
randomized DELAY_MIN_SECONDS-DELAY_MAX_SECONDS between each check a single
worker makes, plus smaller randomized waits/mouse movement within each
check (see _humanize) so actions don't land at identical, instant
intervals. If this still gets blocked, increase the delay further or drop
PREPAY_WORKER_COUNT rather than raising it -- an IP-level block cares
about total concurrent presence from your real IP, which session/profile
rotation can't help with.
"""

import asyncio
import random
import re

from patchright.async_api import TimeoutError as PatchrightTimeoutError
from patchright.async_api import Page, async_playwright

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import prepay_store
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperInterruptedError
from backend.app.scraper.marriott import RATE_CARD_RE, SessionRotator, _build_rates_url

PREPAY_MARKER = "Prepay Non-refundable"
PREPAY_CHUNK_SIZE = 6000

# Each room type's rate cards (including a Prepay Non-refundable one, if it
# has one) sit under a single `<h3 class="standard room-name">` heading for
# that room -- confirmed live 2026-08-29 (e.g. "Guest room, 1 King, Low
# floor"). There's no closing marker tying a rate card to "its" room-name
# element, so the room type for a given match position is whichever
# room-name heading is the closest one *before* it on the page.
ROOM_NAME_RE = re.compile(r'<h3 class="standard room-name">([^<]+)</h3>')
RATE_LOAD_TIMEOUT_MS = 20_000
VIEW_RATES_CLICK_TIMEOUT_MS = 10_000

# The "View Rates" button exists in the DOM right after domcontentloaded
# but its click handler isn't wired up until the page's JS framework
# finishes hydrating -- clicking immediately is a silent no-op (confirmed
# by reproducing with/without a wait against the live site). Randomized
# rather than a fixed wait -- see _humanize().
RATE_PAGE_HYDRATION_WAIT_MS = (2_500, 4_500)
VIEW_RATES_RENDER_WAIT_MS = (2_000, 3_500)

DELAY_MIN_SECONDS = 6.0
DELAY_MAX_SECONDS = 12.0

# How many concurrent browser sessions run prepay checks. Each worker's
# SessionRotator starts on its own freshly-generated random profile (see
# marriott._new_profile_dir), so raising this doesn't need any
# pre-carved-out slices -- independently generated random profiles won't
# collide. Kept at 1 (sequential, no concurrency) after the 2026-08-27
# block -- more concurrent sessions means more concurrent "presence" from
# the same real IP, which is exactly what an IP-level block cares about,
# unlike a session-level block that profile/session rotation can help with.
PREPAY_WORKER_COUNT = 1

# Default cap on how many new hotels a single check_prepay() call checks --
# callers that want more (e.g. the frontend's "check more" button) pass an
# explicit larger limit.
DEFAULT_PREPAY_LIMIT = 10


def _room_type_before(page_html: str, idx: int) -> str | None:
    """The room-name heading immediately preceding position `idx` -- see
    ROOM_NAME_RE for why "immediately preceding" is how a rate card's room
    type is determined."""
    room_type = None
    for match in ROOM_NAME_RE.finditer(page_html, endpos=idx):
        room_type = match.group(1)
    return room_type


async def _humanize(page: Page) -> None:
    """A few small, randomized actions -- move the mouse somewhere, nudge
    the scroll position -- before the next real action, so the automation
    doesn't look like a script clicking the same pixel at an identical
    instant every single time. Deliberately doesn't catch errors here: if
    the browser was closed mid-check, this is exactly where that should
    surface (as a PatchrightError, same as any other page interaction)."""
    await page.mouse.move(random.randint(100, 900), random.randint(100, 700), steps=random.randint(5, 15))
    await page.mouse.wheel(0, random.randint(-150, 300))


def _extract_prepay_offer(page_html: str) -> tuple[float, str | None] | None:
    """Tax-inclusive Member Rate price (and room type) for the Prepay
    Non-refundable plan, if present."""
    idx = page_html.find(PREPAY_MARKER)
    if idx == -1:
        return None
    chunk = page_html[idx : idx + PREPAY_CHUNK_SIZE]
    for rate_name, price_a, price_b in RATE_CARD_RE.findall(chunk):
        if rate_name.strip() == "Member Rate":
            try:
                price = max(float(price_a.replace(",", "")), float(price_b.replace(",", "")))
            except ValueError:
                return None
            return price, _room_type_before(page_html, idx)
    return None


async def _check_hotel_prepay(page: Page, req: SearchRequest, code: str, name: str, nights: int) -> Hotel | None:
    url = _build_rates_url(req, code)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=RATE_LOAD_TIMEOUT_MS)
    if response is not None and response.status == 403:
        raise ScraperBlockedError(f"Marriott returned 403 for {url}")

    await page.wait_for_timeout(random.randint(*RATE_PAGE_HYDRATION_WAIT_MS))
    await _humanize(page)

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

    await page.wait_for_timeout(random.randint(*VIEW_RATES_RENDER_WAIT_MS))
    await _humanize(page)
    offer = _extract_prepay_offer(await page.content())
    if offer is None:
        return None
    price, room_type = offer

    return Hotel(
        name=name,
        price_per_night=price,
        total_price=price * nights,
        currency="USD",
        url=url,
        code=code,
        supports_prepay=True,
        room_type=room_type,
    )


async def _prepay_worker(
    playwright,
    queue: "asyncio.Queue[tuple[str, str]]",
    req: SearchRequest,
    nights: int,
    checked_codes: set[str],
    results: list[Hotel],
) -> None:
    """Pull (code, name) pairs off the shared queue and check each for a
    prepay rate, in its own dedicated browser session (a fresh random
    profile -- see SessionRotator). Runs until the queue is empty. If this
    worker's browser is closed/crashes, it just stops -- other workers
    keep going, and everything checked so far is already saved (see the
    incremental prepay_store.save below).
    """
    async with SessionRotator(playwright) as session:
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

    # Results captured before Hotel gained a room_type field are stuck with
    # supports_prepay=True and room_type=None forever otherwise -- a code
    # already in checked_codes is normally treated as done and never
    # re-checked. Force those back into the queue so a fresh check (which
    # now captures room_type) can fill it in.
    stale_codes = {h.code for h in results if h.code and h.supports_prepay and h.room_type is None}
    if stale_codes:
        checked_codes = checked_codes - stale_codes
        results = [h for h in results if h.code not in stale_codes]

    candidates = [h for h in hotels if h.code and h.code not in checked_codes]
    batch = candidates[:limit] if limit is not None else candidates

    if batch:
        queue: asyncio.Queue = asyncio.Queue()
        for hotel in batch:
            queue.put_nowait((hotel.code, hotel.name))

        async with async_playwright() as playwright:
            await asyncio.gather(
                *[
                    _prepay_worker(playwright, queue, req, nights, checked_codes, results)
                    for _ in range(PREPAY_WORKER_COUNT)
                ]
            )

        checked_codes, results = prepay_store.load(req)

    results_by_code = {h.code: h for h in results if h.code}
    for hotel in hotels:
        if hotel.code in checked_codes:
            match = results_by_code.get(hotel.code)
            hotel.supports_prepay = match is not None
            if match is not None:
                hotel.room_type = match.room_type

    return hotels
