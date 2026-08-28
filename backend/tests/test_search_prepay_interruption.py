import asyncio
from datetime import date
from unittest.mock import AsyncMock

from patchright.async_api import Error as PatchrightError

import backend.app.scraper.marriott_prepay as marriott_prepay_module
from backend.app.models import SearchRequest
from backend.app.scraper import hotel_list_store, prepay_store
from backend.app.scraper.marriott_prepay import search_prepay

REQ = SearchRequest(
    location="New York, NY",
    check_in=date(2026, 9, 11),
    check_out=date(2026, 9, 13),
    adults=1,
    rooms=1,
)


def _property_card(code: str, name: str) -> str:
    data_property = f'{{&quot;hotelName&quot;:&quot;{name}&quot;,&quot;marshacode&quot;:&quot;{code}&quot;}}'
    return f'class=" property-card" data-property="{data_property}">'


class FakeLocator:
    def __init__(self, exists: bool, disabled: bool, click=None):
        self._exists = exists
        self._disabled = disabled
        self.click = click or self._default_click

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self._exists else 0

    async def get_attribute(self, _name):
        return "shop-pagination-next disabled" if self._disabled else "shop-pagination-next"

    async def _default_click(self):
        pass


class FakePage:
    """N pages of results; closes (raises PatchrightError) on the click that
    would advance away from `close_on_page` (None = never closes). Each
    `goto` resets to page 1, like a real fresh navigation would."""

    def __init__(self, pages_content: list[str], close_on_page: int | None = 1):
        self.pages_content = pages_content
        self.state = {"page": 0}
        self.close_on_page = close_on_page
        self.click_count = 0

        async def goto(*args, **kwargs):
            self.state["page"] = 0
            return AsyncMock(status=200)

        self.goto = goto
        self.wait_for_selector = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.title = AsyncMock(return_value="Where Can We Take You?")
        self.evaluate = AsyncMock(return_value=True)

    async def content(self):
        return self.pages_content[self.state["page"]]

    def locator(self, _selector):
        is_last_page = self.state["page"] == len(self.pages_content) - 1
        if self.state["page"] == self.close_on_page:

            async def closed_click():
                raise PatchrightError("Target page, context or browser has been closed")

            return FakeLocator(exists=True, disabled=is_last_page, click=closed_click)

        async def advance():
            self.click_count += 1
            self.state["page"] += 1

        return FakeLocator(exists=True, disabled=is_last_page, click=advance)


class FakeContext:
    def __init__(self, page):
        self.pages = [page]

    async def close(self):
        pass


class FakeChromium:
    def __init__(self, page):
        self._page = page

    async def launch_persistent_context(self, *args, **kwargs):
        return FakeContext(self._page)


class FakePlaywrightCtx:
    def __init__(self, page):
        self.chromium = FakeChromium(page)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        pass


def test_closing_browser_on_page_two_returns_gracefully_and_keeps_results_in_store(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(hotel_list_store, "DATA_DIR", tmp_path / "hotel_list_cache")
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path / "prepay_cache")

    pages_content = [
        _property_card("H1", "Hotel One") + _property_card("H2", "Hotel Two"),
        _property_card("H3", "Hotel Three"),
        _property_card("H4", "Hotel Four"),  # never reached -- browser closes first
    ]
    fake_page = FakePage(pages_content)

    monkeypatch.setattr(
        marriott_prepay_module, "async_playwright", lambda: FakePlaywrightCtx(fake_page)
    )

    # Interrupted during initial listing (before any prepay checks), so no
    # crash and no exception -- just an empty result for this call, same as
    # if it had been called for the very first time with nothing found yet.
    result = asyncio.run(search_prepay(REQ))
    assert result == []

    cached = hotel_list_store.load(REQ)
    assert cached.hotels == [("H1", "Hotel One"), ("H2", "Hotel Two"), ("H3", "Hotel Three")]
    assert cached.pages_fetched == 2
    assert cached.complete is False


def test_second_call_resumes_from_page_three_without_reclicking_pages_one_and_two(
    tmp_path, monkeypatch
):
    """After the interruption above (2 pages already cached), a second call
    for the same query must skip straight to page 3 -- not re-click through
    pages 1 and 2 it already has."""
    monkeypatch.setattr(hotel_list_store, "DATA_DIR", tmp_path / "hotel_list_cache")
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path / "prepay_cache")

    pages_content = [
        _property_card("H1", "Hotel One") + _property_card("H2", "Hotel Two"),
        _property_card("H3", "Hotel Three"),
        _property_card("H4", "Hotel Four"),
    ]

    # First call: closes on page 2 (index 1), same as the scenario above.
    interrupted_page = FakePage(pages_content, close_on_page=1)
    monkeypatch.setattr(
        marriott_prepay_module, "async_playwright", lambda: FakePlaywrightCtx(interrupted_page)
    )
    asyncio.run(search_prepay(REQ))
    assert hotel_list_store.load(REQ).pages_fetched == 2

    # Second call: doesn't close this time. If it resumed correctly (skip_pages=2),
    # reaching page 3 (index 2) takes exactly 2 clicks -- not 2 (skip) + 2 (re-walk).
    resumed_page = FakePage(pages_content, close_on_page=None)
    monkeypatch.setattr(
        marriott_prepay_module, "async_playwright", lambda: FakePlaywrightCtx(resumed_page)
    )
    asyncio.run(search_prepay(REQ, limit=0))  # limit=0: only care about the listing phase here

    final = hotel_list_store.load(REQ)
    assert final.hotels == [
        ("H1", "Hotel One"),
        ("H2", "Hotel Two"),
        ("H3", "Hotel Three"),
        ("H4", "Hotel Four"),
    ]
    assert final.pages_fetched == 3
    assert final.complete is True
    assert resumed_page.click_count == 2
