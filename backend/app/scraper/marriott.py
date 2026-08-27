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
`price` is the per-night rate. Total price is computed from nights * price
since Marriott's search results page does not surface a separate total.
"""

import html
import json
import re
from urllib.parse import quote_plus

from patchright.async_api import TimeoutError as PatchrightTimeoutError
from patchright.async_api import async_playwright

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperTimeoutError

SEARCH_URL_TEMPLATE = (
    "https://www.marriott.com/search/findHotels.mi"
    "?fromDate={from_date}&toDate={to_date}"
    "&destinationAddress.city={city}&destinationAddress.stateProvince={state}"
    "&destinationAddress.country={country}"
    "&roomCount={rooms}&numAdultsPerRoom={adults}"
)

RESULTS_TIMEOUT_MS = 30_000

PROPERTY_CARD_SELECTOR = "div.property-card[data-property]"
DATA_PROPERTY_RE = re.compile(r'data-property="([^"]+)"')

_PROFILE_DIR = "/tmp/hotel_scrape_patchright_profile"


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


def _extract_hotels(page_html: str, nights: int) -> list[Hotel]:
    hotels: list[Hotel] = []
    for raw in DATA_PROPERTY_RE.findall(page_html):
        try:
            data = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue

        name = data.get("hotelName")
        if not name:
            continue

        price_per_night: float | None = None
        raw_price = data.get("price")
        if raw_price:
            try:
                price_per_night = float(raw_price)
            except ValueError:
                price_per_night = None

        total_price = price_per_night * nights if price_per_night is not None else None

        hotels.append(
            Hotel(
                name=name,
                price_per_night=price_per_night,
                total_price=total_price,
                currency=data.get("currency", "USD"),
            )
        )
    return hotels


async def search(req: SearchRequest) -> list[Hotel]:
    """Drive a real (patched) Chromium session through marriott.com search results.

    A legitimate zero-result search returns an empty list rather than raising.

    Raises:
        ScraperBlockedError: the site returned a 403 or an Akamai block page.
        ScraperTimeoutError: the search flow did not reach a results page.
    """
    url = _build_search_url(req)
    nights = (req.check_out - req.check_in).days

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            _PROFILE_DIR,
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
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

            page_html = await page.content()
            return _extract_hotels(page_html, nights)
        finally:
            await context.close()
