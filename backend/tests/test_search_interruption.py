import asyncio
from datetime import date
from unittest.mock import AsyncMock

from patchright.async_api import Error as PatchrightError

import backend.app.scraper.marriott as marriott_module
from backend.app.models import SearchRequest
from backend.app.scraper import search_result_store
from backend.app.scraper.marriott import search

REQ = SearchRequest(
    location="New York, NY",
    check_in=date(2026, 9, 11),
    check_out=date(2026, 9, 13),
    adults=1,
    rooms=1,
)


def _property_card(code: str, name: str, price: str = "469") -> str:
    data_property = (
        f'{{&quot;currency&quot;:&quot;USD&quot;,&quot;price&quot;:&quot;{price}&quot;,'
        f'&quot;hotelName&quot;:&quot;{name}&quot;,&quot;marshacode&quot;:&quot;{code}&quot;}}'
    )
    return (
        f'class=" property-card" data-property="{data_property}">'
        f'<span aria-hidden="false" class="m-price" aria-label="  now  {price}  ">{price}</span>'
    )


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
    """N full pages of results; closes (raises PatchrightError) on the click
    that would advance away from `close_on_page` (None = never closes).
    Each `goto` resets to page 1, like a real fresh navigation would."""

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


def test_closing_browser_on_page_two_returns_page_one_results_instead_of_raising(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(search_result_store, "DATA_DIR", tmp_path)

    pages_content = [
        _property_card("H1", "Hotel One", "469") + _property_card("H2", "Hotel Two", "549"),
        _property_card("H3", "Hotel Three", "431"),
        _property_card("H4", "Hotel Four", "612"),  # never reached -- browser closes first
    ]
    fake_page = FakePage(pages_content)

    monkeypatch.setattr(marriott_module, "async_playwright", lambda: FakePlaywrightCtx(fake_page))

    result = asyncio.run(search(REQ))

    assert [h.name for h in result] == ["Hotel One", "Hotel Two", "Hotel Three"]

    cached = search_result_store.load(REQ)
    assert cached.pages_fetched == 2
    assert cached.complete is False


def test_second_call_resumes_from_page_three_without_reclicking_pages_one_and_two(
    tmp_path, monkeypatch
):
    """After the interruption above (2 pages already cached), a second call
    for the same query must skip straight to page 3 -- not re-click through
    pages 1 and 2 it already has."""
    monkeypatch.setattr(search_result_store, "DATA_DIR", tmp_path)

    pages_content = [
        _property_card("H1", "Hotel One", "469") + _property_card("H2", "Hotel Two", "549"),
        _property_card("H3", "Hotel Three", "431"),
        _property_card("H4", "Hotel Four", "612"),
    ]

    interrupted_page = FakePage(pages_content, close_on_page=1)
    monkeypatch.setattr(
        marriott_module, "async_playwright", lambda: FakePlaywrightCtx(interrupted_page)
    )
    asyncio.run(search(REQ))
    assert search_result_store.load(REQ).pages_fetched == 2

    resumed_page = FakePage(pages_content, close_on_page=None)
    monkeypatch.setattr(marriott_module, "async_playwright", lambda: FakePlaywrightCtx(resumed_page))
    result = asyncio.run(search(REQ))

    assert [h.name for h in result] == ["Hotel One", "Hotel Two", "Hotel Three", "Hotel Four"]
    final = search_result_store.load(REQ)
    assert final.pages_fetched == 3
    assert final.complete is True
    assert resumed_page.click_count == 2
