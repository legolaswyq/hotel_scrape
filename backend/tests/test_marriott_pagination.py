import asyncio
from datetime import date
from unittest.mock import AsyncMock

from backend.app.models import SearchRequest
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


def test_list_all_hotels_walks_all_pages_and_dedupes():
    pages_content = [
        _property_card("H1", "Hotel One") + _property_card("H2", "Hotel Two"),
        _property_card("H2", "Hotel Two") + _property_card("H3", "Hotel Three"),
    ]
    page = FakePage(pages_content)

    result = asyncio.run(list_all_hotels(page, REQ))

    assert [h.name for h in result] == ["Hotel One", "Hotel Two", "Hotel Three"]
