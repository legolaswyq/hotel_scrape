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


def _extract_hotel_codes(page_html: str) -> list[tuple[str, str]]:
    """Return (marshacode, hotelName) for each result card, in listed order."""
    codes: list[tuple[str, str]] = []
    for chunk in page_html.split(PROPERTY_CARD_SPLIT)[1:]:
        prop_match = DATA_PROPERTY_RE.search(chunk)
        if not prop_match:
            continue
        try:
            data = json.loads(html.unescape(prop_match.group(1)))
        except json.JSONDecodeError:
            continue
        code = data.get("marshacode")
        name = data.get("hotelName")
        if code and name:
            codes.append((code, name))
    return codes


def _extract_hotels(page_html: str, req: SearchRequest, nights: int) -> list[Hotel]:
    hotels: list[Hotel] = []
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
            Hotel(
                name=name,
                price_per_night=price_per_night,
                total_price=total_price,
                currency=data.get("currency", "USD"),
                url=url,
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
            return _extract_hotels(page_html, req, nights)
        finally:
            await context.close()
