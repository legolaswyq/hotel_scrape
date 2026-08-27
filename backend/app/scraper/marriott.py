"""Playwright-driven scraper for marriott.com hotel search results.

NOTE ON SELECTOR PROVENANCE (read before touching this file):
Live verification against marriott.com was attempted for this task and blocked.
A headless Chromium request to https://www.marriott.com/default.mi via Playwright
(not a plain HTTP client) received an immediate 403 from Akamai's bot-protection
layer, with `Retry-After: 28800` (8 hours) -- the same block observed earlier via
plain curl on this network. Because retrying against a live block risks extending
it further, `playwright codegen` was not run against the live site, and the
selectors below were NOT captured from a real recorded session.

The selectors in this module are inferred from general knowledge of
marriott.com's search UI (destination input, autosuggest dropdown, date
pickers, guest/room selectors, results cards) and are UNVERIFIED. They are a
best-effort starting structure only. Before relying on this scraper, someone
must re-run `playwright codegen https://www.marriott.com/default.mi` once the
block has lifted (or from a different network), and update:
  - the destination input / autosuggest option selectors
  - the date field selectors and the date-entry interaction (Marriott's date
    picker is typically a calendar widget, not a plain text fill)
  - the guest/room selectors (often behind a "Rooms & Guests" popover control
    rather than plain <select> elements)
  - the results-card selectors and per-card name/price/total selectors
  - the no-results indicator selector (see NO_RESULTS_SELECTOR below) --
    Marriott search UIs commonly render a "no properties match your search"
    or similar message when a search legitimately returns zero hotels; this
    must be distinguished from a genuine timeout/failure so that a
    zero-result search returns `hotels: []` (200) rather than being reported
    as ScraperTimeoutError (502). The selector below is an unverified
    best-effort guess, like the others in this file.
using `page.pause()` or a scratch script to inspect the live DOM.

STEALTH NOTE: this module uses playwright-stealth (Stealth().use_async(...))
plus a launch arg disabling the AutomationControlled blink feature, to reduce
common headless-browser fingerprints Akamai and similar bot-protection
vendors check for (navigator.webdriver, missing plugins/fonts, automation
flags). This narrows the gap versus a real browser but is not a guarantee of
avoiding detection -- Marriott may still block the session.
"""

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperTimeoutError
from backend.app.scraper.parsing import parse_price

SEARCH_URL = "https://www.marriott.com/default.mi"
RESULTS_TIMEOUT_MS = 30_000

HOTEL_CARD_SELECTOR = "[data-testid='hotel-card']"
# UNVERIFIED: see module docstring. Best-effort placeholder for whatever
# element Marriott renders when a search legitimately returns zero results.
NO_RESULTS_SELECTOR = "[data-testid='no-results']"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


async def search(req: SearchRequest) -> list[Hotel]:
    """Drive a real Chromium session through marriott.com's search UI.

    A legitimate zero-result search (results area loaded, no hotel cards
    present) returns an empty list rather than raising -- this is a valid
    outcome, not a failure.

    Raises:
        ScraperBlockedError: the site returned a 403 or otherwise blocked the
            session (Akamai bot-protection, CAPTCHA challenge page, etc).
        ScraperTimeoutError: the search flow did not reach a results page at
            all -- neither hotel cards nor the no-results indicator appeared
            within RESULTS_TIMEOUT_MS.
    """
    async with Stealth().use_async(async_playwright()) as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = await browser.new_page(
                user_agent=_USER_AGENT,
                viewport={"width": 1366, "height": 900},
            )
            response = await page.goto(SEARCH_URL, wait_until="domcontentloaded")
            if response is not None and response.status == 403:
                raise ScraperBlockedError(f"Marriott returned 403 for {SEARCH_URL}")

            # --- UNVERIFIED: inferred selectors, see module docstring. ---
            await page.get_by_placeholder("Destination").fill(req.location)
            await page.get_by_role("option").first.click()
            await page.get_by_label("Check-in date").fill(req.check_in.strftime("%m/%d/%Y"))
            await page.get_by_label("Check-out date").fill(req.check_out.strftime("%m/%d/%Y"))
            await page.get_by_label("Adults").select_option(str(req.adults))
            await page.get_by_label("Rooms").select_option(str(req.rooms))
            await page.get_by_role("button", name="Search").click()

            # Wait for whichever results-area signal appears first: either
            # hotel cards (results found) or the no-results indicator (a
            # legitimate zero-result search). Only a genuine timeout -- where
            # neither signal ever appears -- is treated as ScraperTimeoutError.
            await page.wait_for_selector(
                f"{HOTEL_CARD_SELECTOR}, {NO_RESULTS_SELECTOR}",
                timeout=RESULTS_TIMEOUT_MS,
            )

            cards = await page.query_selector_all(HOTEL_CARD_SELECTOR)
            if not cards:
                return []

            hotels: list[Hotel] = []
            for card in cards:
                name_el = await card.query_selector("[data-testid='hotel-name']")
                price_el = await card.query_selector("[data-testid='price-per-night']")
                total_el = await card.query_selector("[data-testid='total-price']")

                name = (await name_el.inner_text()).strip() if name_el else None
                price_text = await price_el.inner_text() if price_el else None
                total_text = await total_el.inner_text() if total_el else None

                if not name:
                    continue

                hotels.append(
                    Hotel(
                        name=name,
                        price_per_night=parse_price(price_text),
                        total_price=parse_price(total_text),
                        currency="USD",
                    )
                )

            return hotels
        except PlaywrightTimeoutError as exc:
            raise ScraperTimeoutError(
                f"Timed out waiting for Marriott results for '{req.location}'"
            ) from exc
        finally:
            await browser.close()
