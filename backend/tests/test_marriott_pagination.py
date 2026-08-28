import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PatchrightTimeoutError

from backend.app.models import SearchRequest
from backend.app.scraper.exceptions import ScraperBlockedError
from backend.app.scraper.marriott import list_all_hotel_codes, list_all_hotels

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
    def __init__(self, exists: bool, disabled: bool, on_click=None):
        self._exists = exists
        self._disabled = disabled
        self._on_click = on_click

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self._exists else 0

    async def get_attribute(self, _name):
        return "shop-pagination-next disabled" if self._disabled else "shop-pagination-next"

    async def click(self):
        if self._on_click:
            self._on_click()


class FakePage:
    """Simulates N pages of results; each `next` click advances `state["page"]`."""

    def __init__(self, pages_content: list[str]):
        self.pages_content = pages_content
        self.state = {"page": 0}
        self.goto = AsyncMock(return_value=AsyncMock(status=200))
        self.wait_for_selector = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.title = AsyncMock(return_value="Where Can We Take You?")
        self.evaluate = AsyncMock(return_value=True)

    async def content(self):
        return self.pages_content[self.state["page"]]

    def locator(self, _selector):
        is_last_page = self.state["page"] == len(self.pages_content) - 1

        def advance():
            self.state["page"] += 1

        return FakeLocator(exists=True, disabled=is_last_page, on_click=advance)


def test_walks_all_pages_and_dedupes():
    pages_content = [
        _property_card("H1", "Hotel One") + _property_card("H2", "Hotel Two"),
        _property_card("H3", "Hotel Three"),
        _property_card("H3", "Hotel Three") + _property_card("H4", "Hotel Four"),
    ]
    page = FakePage(pages_content)

    result = asyncio.run(list_all_hotel_codes(page, REQ))

    assert result == [
        ("H1", "Hotel One"),
        ("H2", "Hotel Two"),
        ("H3", "Hotel Three"),
        ("H4", "Hotel Four"),
    ]


def test_stops_when_next_link_absent():
    pages_content = [_property_card("H1", "Hotel One")]
    page = FakePage(pages_content)
    page.locator = lambda _selector: FakeLocator(exists=False, disabled=False)

    result = asyncio.run(list_all_hotel_codes(page, REQ))

    assert result == [("H1", "Hotel One")]


def test_list_all_hotel_codes_reports_progress_after_each_page():
    pages_content = [
        _property_card("H1", "Hotel One") + _property_card("H2", "Hotel Two"),
        _property_card("H3", "Hotel Three"),
    ]
    page = FakePage(pages_content)
    snapshots = []

    asyncio.run(list_all_hotel_codes(page, REQ, on_progress=snapshots.append))

    assert snapshots == [
        [("H1", "Hotel One"), ("H2", "Hotel Two")],
        [("H1", "Hotel One"), ("H2", "Hotel Two"), ("H3", "Hotel Three")],
    ]


def test_list_all_hotels_walks_all_pages_and_dedupes():
    pages_content = [
        _property_card("H1", "Hotel One") + _property_card("H2", "Hotel Two"),
        _property_card("H2", "Hotel Two") + _property_card("H3", "Hotel Three"),
    ]
    page = FakePage(pages_content)

    result = asyncio.run(list_all_hotels(page, REQ))

    assert [h.name for h in result] == ["Hotel One", "Hotel Two", "Hotel Three"]


def test_block_mid_pagination_raises_instead_of_silently_truncating():
    """A block after a 'Next' click used to look identical to 'no more
    hotels' (no cards render, so nothing gets added) and would silently
    return a truncated-but-marked-complete list. It must now surface as a
    ScraperBlockedError instead."""
    pages_content = [_property_card("H1", "Hotel One"), _property_card("H2", "Hotel Two")]
    page = FakePage(pages_content)

    call_count = {"n": 0}
    real_wait_for_selector = page.wait_for_selector

    async def wait_for_selector_then_block(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return await real_wait_for_selector(*args, **kwargs)
        raise PatchrightTimeoutError("timed out")

    page.wait_for_selector = wait_for_selector_then_block
    page.title = AsyncMock(return_value="Access Denied")

    with pytest.raises(ScraperBlockedError):
        asyncio.run(list_all_hotel_codes(page, REQ))


def test_closing_browser_on_page_two_keeps_pages_one_and_two_via_on_progress():
    """Regression for: closing the browser window while on page 2 must not
    lose page 1's (or page 2's own, since it already finished loading)
    results -- only the not-yet-fetched page 3 onward is lost. Simulates
    the close as a raw patchright Error (what a manually closed window
    raises) hitting the click that would advance to page 3."""
    pages_content = [
        _property_card("H1", "Hotel One") + _property_card("H2", "Hotel Two"),
        _property_card("H3", "Hotel Three"),
        _property_card("H4", "Hotel Four"),  # never reached -- browser closes first
    ]
    page = FakePage(pages_content)

    real_click = None

    def locator_that_closes_on_third_click(_selector):
        nonlocal real_click
        is_last_page = page.state["page"] == len(pages_content) - 1

        def advance():
            page.state["page"] += 1

        locator = FakeLocator(exists=True, disabled=is_last_page, on_click=advance)
        if page.state["page"] == 1:
            # We're on page 2 already; the click to advance to page 3 is
            # where the manually-closed browser raises.
            async def closed_click():
                raise PatchrightError("Target page, context or browser has been closed")

            locator.click = closed_click
        return locator

    page.locator = locator_that_closes_on_third_click

    snapshots = []
    with pytest.raises(PatchrightError):
        asyncio.run(list_all_hotel_codes(page, REQ, on_progress=snapshots.append))

    # Page 1 and page 2's hotels were captured via on_progress before the
    # close interrupted the click to page 3.
    assert snapshots[-1] == [
        ("H1", "Hotel One"),
        ("H2", "Hotel Two"),
        ("H3", "Hotel Three"),
    ]
