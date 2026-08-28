"""Patchright-driven scraper for marriott.com hotel search results.

NOTE ON SELECTOR PROVENANCE:
Verified live against marriott.com on 2026-08-27. Plain HTTP and stock
Playwright (any channel, headless or headed) are blocked by Akamai
("Access Denied", errors.edgesuite.net) -- this appears to be detection of
Chrome DevTools Protocol automation signals, not headless fingerprints or
missing cookies (a copy of a real, logged-in Chrome profile was blocked
identically). Switching to patchright (a Playwright fork that patches the
CDP leaks Akamai/Cloudflare-style detectors key on) gets a real results
page through, using a fresh dedicated profile -- no real user profile
needed.

Each result card is a `div.property-card[data-property='{...}']` element
where the `data-property` attribute is an HTML-escaped JSON blob:
    {"lat":..., "long":..., "brand":"CY", "marshacode":"NYCES",
     "currency":"USD", "price":"469", "hotelName":"Courtyard by ..."}
`data-property.price` is always the pre-tax base rate and does NOT reflect
the "show rates with taxes and all fees" toggle (`showFullPrice=true` in
the URL), even when that toggle is on. The tax-inclusive rate is only in
the rendered price span within the same card, as `aria-label="  now  541  "`
on the `.m-price` element. So price is read from that span per-card, not
from `data-property`; `data-property` is only used for the hotel name
(and currency). Total price is computed from nights * price since
Marriott's search results page does not surface a separate total.
"""

import html
import json
import re
import uuid
from urllib.parse import quote_plus

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PatchrightTimeoutError
from patchright.async_api import async_playwright

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import search_result_store
from backend.app.scraper.exceptions import (
    ScraperBlockedError,
    ScraperInterruptedError,
    ScraperTimeoutError,
)

SEARCH_URL_TEMPLATE = (
    "https://www.marriott.com/search/findHotels.mi"
    "?fromDate={from_date}&toDate={to_date}"
    "&destinationAddress.city={city}&destinationAddress.stateProvince={state}"
    "&destinationAddress.country={country}"
    "&roomCount={rooms}&numAdultsPerRoom={adults}"
    "&showAvailableHotels=true&showFullPrice=true"
)

RATES_URL_TEMPLATE = (
    "https://www.marriott.com/search/availabilityCalendar.mi"
    "?propertyCode={code}&isSearch=true&showFullPrice=true"
    "&fromDate={from_date}&toDate={to_date}"
    "&roomCount={rooms}&numAdultsPerRoom={adults}"
)

RESULTS_TIMEOUT_MS = 30_000

PROPERTY_CARD_SELECTOR = "div.property-card[data-property]"
PROPERTY_CARD_SPLIT = 'class=" property-card"'
DATA_PROPERTY_RE = re.compile(r'data-property="([^"]+)"')
DISPLAYED_PRICE_RE = re.compile(r'aria-label="\s*now\s*([\d,]+)\s*"')

# Matches a rate-plan card's name and its two price spans (tax-inclusive and
# pre-tax base price). Which span carries the `d-none` (hidden) class flips
# depending on page state/navigation path -- confirmed live, not just a
# theoretical toggle -- so this captures both numbers by position only and
# leaves picking the tax-inclusive one (the larger of the two) to the
# caller, rather than trusting the class. Used by marriott_prepay.py to
# read a specific rate plan's price after expanding a room's "View Rates".
RATE_CARD_RE = re.compile(
    r'rate-name">([^<]+)</span>.*?class="price">'
    r'<span aria-hidden="false" class="[^"]*">([\d,]+)</span>'
    r'<span aria-hidden="false" class="[^"]*">([\d,]+)</span>',
    re.S,
)

_PROFILE_DIR = "/tmp/hotel_scrape_patchright_profile_2"

# Session rotation: when Akamai blocks the current profile, closing that
# context and opening a fresh one has recovered live, confirmed 2026-08-28
# (a burst that got a profile blocked was unblocked by switching to a new,
# never-used profile directory -- the block is keyed to session state, not
# our IP). Rotated-to profiles are generated on demand -- a fresh,
# randomly-named directory each time -- rather than drawn from a fixed,
# reused pool of numbered paths (`_rot1`, `_rot2`, ...): a predictable set
# of profile paths reused across every run is itself a fingerprint, and
# it's what made concurrent sessions need pre-carved-out disjoint slices
# to avoid colliding. MAX_PROFILE_ROTATIONS bounds how many a single call
# will burn through before giving up and raising -- if that many fresh,
# never-before-used profiles are all still blocked, that's a stronger
# signal (network/IP-level) that rotating further won't help.
MAX_PROFILE_ROTATIONS = 5


def _new_profile_dir() -> str:
    """A fresh, never-before-used browser profile directory."""
    return f"{_PROFILE_DIR}_{uuid.uuid4().hex[:12]}"

# Search results are paginated (40 hotels/page). Pagination is client-side
# (an `<a href="#">` with a click handler, not a real navigation) --
# confirmed live on 2026-08-28. The "NextPage" link gains a `disabled`
# class on the last page (same pattern as "PrevPage" on page 1).
NEXT_PAGE_SELECTOR = 'a[aria-label="NextPage"]'
PAGE_HYDRATION_WAIT_MS = 3_000
PAGINATION_CLICK_WAIT_MS = 2_500
MAX_RESULT_PAGES = 20

# Price labels for a page's cards render staggered, slightly after the
# cards themselves -- confirmed live: reading page content right after
# cards appear (single fixed wait) left many cards with no price captured,
# worse after pagination clicks than on the first page. Poll until the
# number of rendered price labels stops increasing instead of guessing a
# fixed wait long enough for every card.
PRICE_POLL_INTERVAL_MS = 1_000
PRICE_POLL_MAX_ATTEMPTS = 6

# Card prices lazy-render only once scrolled into view -- confirmed live: a
# freshly loaded results page has prices for only the first ~10 of 40 cards
# until scrolled (images and other card content likely lazy too, but price
# is what we extract). Scroll down in increments, not straight to the
# bottom, so every card actually passes through the viewport and triggers
# whatever intersection-based loading Marriott's page uses.
SCROLL_STEP_PX = 2_000
SCROLL_STEP_WAIT_MS = 500
SCROLL_MAX_STEPS = 30


def _parse_location(location: str) -> tuple[str, str]:
    """Split 'City, ST' into (city, state). Only US 'City, ST' input is supported."""
    parts = [p.strip() for p in location.split(",")]
    city = parts[0]
    state = parts[1] if len(parts) > 1 else ""
    return city, state


def _build_search_url(req: SearchRequest) -> str:
    city, state = _parse_location(req.location)
    return SEARCH_URL_TEMPLATE.format(
        from_date=quote_plus(req.check_in.strftime("%m/%d/%Y")),
        to_date=quote_plus(req.check_out.strftime("%m/%d/%Y")),
        city=quote_plus(city),
        state=quote_plus(state),
        country="US",
        rooms=req.rooms,
        adults=req.adults,
    )


def _build_rates_url(req: SearchRequest, code: str) -> str:
    """URL for a specific hotel's room/rate picker page, given its marshacode."""
    return RATES_URL_TEMPLATE.format(
        code=code,
        from_date=quote_plus(req.check_in.strftime("%m/%d/%Y")),
        to_date=quote_plus(req.check_out.strftime("%m/%d/%Y")),
        rooms=req.rooms,
        adults=req.adults,
    )


class SessionRotator:
    """Runs work against a patchright page, auto-rotating to a fresh browser
    profile and retrying when a ScraperBlockedError is raised, instead of
    failing the whole call on the first block encountered.

    If the browser window is closed manually (or the browser process
    crashes) mid-scrape, that's a different failure mode -- not a site
    block -- and is NOT auto-retried (rotating and popping open a new
    window would fight a user who closed it on purpose). It surfaces as
    ScraperInterruptedError instead of an unhandled patchright error.

    Pass `base_profile_dir` to start on a specific, stable profile (e.g.
    the app's default one, which accumulates real cookies/history across
    calls and reads more like a returning visitor than a throwaway one).
    Leave it unset to start on a fresh random profile instead -- needed
    when running several SessionRotators concurrently (e.g. parallel
    prepay-check workers), since there's then no shared starting point
    they could collide on. Every rotation after that (regardless of
    `base_profile_dir`) always generates a brand new random profile --
    never reused, never drawn from a fixed list.

    Usage:
        async with SessionRotator(playwright) as session:
            result = await session.run(lambda page: some_scrape_fn(page, ...))
    """

    def __init__(
        self,
        playwright,
        base_profile_dir: str | None = None,
        max_rotations: int = MAX_PROFILE_ROTATIONS,
    ):
        self._playwright = playwright
        self._base_profile_dir = base_profile_dir
        self._max_rotations = max_rotations
        self._rotations = 0
        self.context = None
        self.page = None

    async def __aenter__(self) -> "SessionRotator":
        await self._launch(self._base_profile_dir or _new_profile_dir())
        return self

    async def __aexit__(self, *exc_info):
        if self.context is not None:
            await self._close_context()

    async def _close_context(self) -> None:
        try:
            await self.context.close()
        except PatchrightError:
            # Already gone (e.g. the window was closed manually) -- fine,
            # that's exactly the state we were trying to reach.
            pass

    async def _launch(self, profile_dir: str) -> None:
        self.context = await self._playwright.chromium.launch_persistent_context(
            profile_dir,
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def _rotate(self) -> None:
        self._rotations += 1
        if self._rotations >= self._max_rotations:
            raise ScraperBlockedError(
                f"Still blocked after rotating through {self._rotations} browser profiles"
            )
        await self._close_context()
        await self._launch(_new_profile_dir())

    async def run(self, coro_fn):
        """Call coro_fn(page), rotating profile and retrying on ScraperBlockedError.

        Raises:
            ScraperInterruptedError: the browser session ended unexpectedly
                (window closed manually, browser crashed) -- not retried.
        """
        while True:
            try:
                return await coro_fn(self.page)
            except ScraperBlockedError:
                await self._rotate()
            except PatchrightError as exc:
                raise ScraperInterruptedError(
                    "Browser session ended unexpectedly (window closed or browser crashed)"
                ) from exc


async def _scroll_through_page(page) -> None:
    """Step down the page in increments until reaching the bottom (or the
    safety cap), so every card passes through the viewport."""
    for _ in range(SCROLL_MAX_STEPS):
        reached_bottom = await page.evaluate(
            f"() => {{ const before = window.scrollY; "
            f"window.scrollBy(0, {SCROLL_STEP_PX}); "
            f"return window.scrollY === before; }}"
        )
        await page.wait_for_timeout(SCROLL_STEP_WAIT_MS)
        if reached_bottom:
            break


async def _wait_for_stable_prices(page) -> str:
    """Scroll through the page (prices lazy-render into view) then poll
    page content until the count of rendered price labels stops growing."""
    await _scroll_through_page(page)
    previous_count = -1
    content = await page.content()
    for _ in range(PRICE_POLL_MAX_ATTEMPTS):
        current_count = len(DISPLAYED_PRICE_RE.findall(content))
        if current_count == previous_count:
            break
        previous_count = current_count
        await page.wait_for_timeout(PRICE_POLL_INTERVAL_MS)
        content = await page.content()
    return content


def _extract_hotels(page_html: str, req: SearchRequest, nights: int) -> list[tuple[str | None, Hotel]]:
    """Return (marshacode, Hotel) per result card -- the code is exposed so
    callers can dedupe across multiple pages of results."""
    hotels: list[tuple[str | None, Hotel]] = []
    for chunk in page_html.split(PROPERTY_CARD_SPLIT)[1:]:
        prop_match = DATA_PROPERTY_RE.search(chunk)
        if not prop_match:
            continue
        try:
            data = json.loads(html.unescape(prop_match.group(1)))
        except json.JSONDecodeError:
            continue

        name = data.get("hotelName")
        if not name:
            continue

        price_per_night: float | None = None
        price_match = DISPLAYED_PRICE_RE.search(chunk)
        if price_match:
            try:
                price_per_night = float(price_match.group(1).replace(",", ""))
            except ValueError:
                price_per_night = None

        total_price = price_per_night * nights if price_per_night is not None else None

        code = data.get("marshacode")
        url = _build_rates_url(req, code) if code else None

        hotels.append(
            (
                code,
                Hotel(
                    name=name,
                    price_per_night=price_per_night,
                    total_price=total_price,
                    currency=data.get("currency", "USD"),
                    url=url,
                    code=code,
                ),
            )
        )
    return hotels


async def _advance_to_next_page(page, req: SearchRequest) -> bool:
    """Click 'Next'. Returns False if there is no next page (already at the
    end -- nothing to do). Raises if the click doesn't lead to a page of
    cards (block or timeout), which used to look identical to "reached the
    end" and silently corrupt the result as false-complete.
    """
    next_link = page.locator(NEXT_PAGE_SELECTOR).first
    if await next_link.count() == 0:
        return False
    classes = await next_link.get_attribute("class") or ""
    if "disabled" in classes:
        return False

    await next_link.click()
    # Clicking briefly clears the results area entirely before the next
    # page's cards render -- confirmed live: a fixed wait alone can land in
    # that empty window. Wait for cards to reappear, then settle.
    try:
        await page.wait_for_selector(PROPERTY_CARD_SELECTOR, timeout=RESULTS_TIMEOUT_MS)
    except PatchrightTimeoutError as exc:
        title = await page.title()
        if "access denied" in title.lower():
            raise ScraperBlockedError("Marriott blocked pagination mid-scan") from exc
        raise ScraperTimeoutError(
            f"Timed out waiting for the next page of results for '{req.location}'"
        ) from exc
    await page.wait_for_timeout(PAGINATION_CLICK_WAIT_MS)
    return True


async def _walk_result_pages(page, req: SearchRequest, skip_pages: int = 0):
    """Navigate to search results and yield each page's HTML in turn, clicking
    through pagination until the last page (or MAX_RESULT_PAGES as a safety cap).

    `skip_pages` clicks through that many pages first WITHOUT yielding them
    (no price-scroll/settle wait either, since their content is discarded)
    -- for resuming a scan that already captured those pages in an earlier
    call, without re-extracting them.

    The caller decides when to stop early (e.g. a page that added nothing new) by
    simply not continuing the `async for` loop.

    Raises:
        ScraperBlockedError: the site returned a 403 or an Akamai block page.
        ScraperTimeoutError: the search flow did not reach a results page.
    """
    url = _build_search_url(req)
    response = await page.goto(url, wait_until="domcontentloaded")
    if response is not None and response.status == 403:
        raise ScraperBlockedError(f"Marriott returned 403 for {url}")

    try:
        await page.wait_for_selector(PROPERTY_CARD_SELECTOR, timeout=RESULTS_TIMEOUT_MS)
    except PatchrightTimeoutError as exc:
        title = await page.title()
        if "access denied" in title.lower():
            raise ScraperBlockedError(f"Marriott blocked the request for {url}") from exc
        raise ScraperTimeoutError(
            f"Timed out waiting for Marriott results for '{req.location}'"
        ) from exc

    # Same hydration issue as the rate page's "View Rates" button: the
    # pagination link exists in the DOM before its click handler is wired up.
    await page.wait_for_timeout(PAGE_HYDRATION_WAIT_MS)

    for _ in range(skip_pages):
        if not await _advance_to_next_page(page, req):
            # Fewer pages now than when skip_pages was recorded (results
            # shifted) -- nothing left to skip past.
            return

    for _ in range(MAX_RESULT_PAGES):
        yield await _wait_for_stable_prices(page)
        if not await _advance_to_next_page(page, req):
            return


async def list_all_hotels(
    page, req: SearchRequest, on_progress=None, skip_pages: int = 0
) -> list[Hotel]:
    """Walk every results page (after skipping `skip_pages` already-known
    ones), returning a Hotel per result found on the pages actually walked,
    deduplicated by marshacode (cards without a marshacode are kept as-is,
    undeduped).

    If `on_progress` is given, it's called with the accumulated list after
    each page (not just at the end) -- so a caller that persists it can
    recover whatever was found so far even if a later page raises (e.g. a
    block partway through a long pagination walk).

    Raises:
        ScraperBlockedError: the site returned a 403 or an Akamai block page.
        ScraperTimeoutError: the search flow did not reach a results page.
    """
    nights = (req.check_out - req.check_in).days
    seen_codes: set[str] = set()
    hotels: list[Hotel] = []

    async for page_html in _walk_result_pages(page, req, skip_pages=skip_pages):
        added = False
        for code, hotel in _extract_hotels(page_html, req, nights):
            if code and code in seen_codes:
                continue
            if code:
                seen_codes.add(code)
            hotels.append(hotel)
            added = True
        if on_progress is not None:
            on_progress(list(hotels))
        if not added:
            break

    return hotels


def _merge_hotels(known: list[Hotel], new: list[Hotel]) -> list[Hotel]:
    """Combine already-cached hotels with freshly fetched ones, deduping by
    name (Hotel has no marshacode field -- name is a reasonable proxy)."""
    seen_names = {h.name for h in known}
    merged = list(known)
    for hotel in new:
        if hotel.name not in seen_names:
            seen_names.add(hotel.name)
            merged.append(hotel)
    return merged


async def search(req: SearchRequest) -> list[Hotel]:
    """Drive a real (patched) Chromium session through every page of marriott.com
    search results, auto-rotating to a fresh browser profile and retrying if
    blocked (see SessionRotator).

    A legitimate zero-result search returns an empty list rather than raising.

    Resumable across calls: progress (hotels found so far, how many pages
    were walked, whether the listing is complete) persists to a local JSON
    cache (search_result_store.py) keyed by location/dates/guest count. A
    later call for the same query -- e.g. after the browser window was
    closed manually partway through -- skips straight past the pages
    already fetched instead of re-walking them from page 1. Once a listing
    is marked complete, later calls just return the cached list.

    Raises:
        ScraperBlockedError: still blocked after exhausting the profile pool.
        ScraperTimeoutError: the search flow did not reach a results page.
    """
    known_hotels, pages_fetched, complete = search_result_store.load(req)
    if complete:
        return known_hotels

    pages_done_this_call = 0

    def on_progress(hotels_so_far: list[Hotel]) -> None:
        nonlocal pages_done_this_call
        pages_done_this_call += 1
        combined = _merge_hotels(known_hotels, hotels_so_far)
        search_result_store.save(
            req, combined, pages_fetched + pages_done_this_call, complete=False
        )

    async with async_playwright() as playwright:
        async with SessionRotator(playwright, base_profile_dir=_PROFILE_DIR) as session:
            try:
                new_hotels = await session.run(
                    lambda page: list_all_hotels(
                        page, req, on_progress=on_progress, skip_pages=pages_fetched
                    )
                )
            except ScraperInterruptedError:
                # Whatever on_progress already saved is the best we have.
                return search_result_store.load(req).hotels

    final = _merge_hotels(known_hotels, new_hotels)
    search_result_store.save(req, final, pages_fetched + pages_done_this_call, complete=True)
    return final
