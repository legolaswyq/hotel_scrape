import asyncio
from datetime import date
from unittest.mock import AsyncMock

from backend.app.models import SearchRequest
from backend.app.scraper.marriott import _build_rates_url, _extract_hotels, _wait_for_stable_prices

REQ = SearchRequest(
    location="New York, NY",
    check_in=date(2026, 9, 11),
    check_out=date(2026, 9, 13),
    adults=1,
    rooms=1,
)


def test_build_rates_url_includes_property_code_and_dates():
    url = _build_rates_url(REQ, "NYCES")
    assert "propertyCode=NYCES" in url
    assert "fromDate=09%2F11%2F2026" in url
    assert "toDate=09%2F13%2F2026" in url
    assert "roomCount=1" in url
    assert "numAdultsPerRoom=1" in url


def _property_card(name: str, marshacode: str, price: str) -> str:
    data_property = (
        f'{{&quot;currency&quot;:&quot;USD&quot;,&quot;price&quot;:&quot;{price}&quot;,'
        f'&quot;hotelName&quot;:&quot;{name}&quot;,&quot;marshacode&quot;:&quot;{marshacode}&quot;}}'
    )
    return (
        f'class=" property-card" data-property="{data_property}">'
        f'<span aria-hidden="false" class="m-price" aria-label="  now  {price}  ">{price}</span>'
    )


def test_extract_hotels_includes_rate_page_url():
    page_html = _property_card("Courtyard Test", "NYCES", "469")
    hotels = _extract_hotels(page_html, REQ, nights=2)
    assert len(hotels) == 1
    code, hotel = hotels[0]
    assert code == "NYCES"
    assert hotel.name == "Courtyard Test"
    assert hotel.url is not None
    assert "propertyCode=NYCES" in hotel.url


def test_extract_hotels_url_is_none_without_marshacode():
    page_html = (
        'class=" property-card" data-property="{&quot;hotelName&quot;:&quot;No Code Hotel&quot;}">'
        '<span aria-hidden="false" aria-label="  now  100  ">100</span>'
    )
    hotels = _extract_hotels(page_html, REQ, nights=2)
    assert len(hotels) == 1
    code, hotel = hotels[0]
    assert code is None
    assert hotel.url is None


def _price_labels(count: int) -> str:
    return "".join(f'aria-label="  now  {100 + i}  "' for i in range(count))


class FakePricePage:
    """Simulates prices rendering in over successive .content() reads, then
    stabilizing at `final_count`."""

    def __init__(self, growth: list[int]):
        self._growth = growth
        self._call = 0
        self.wait_for_timeout = AsyncMock()
        self.evaluate = AsyncMock(return_value=True)

    async def content(self):
        count = self._growth[min(self._call, len(self._growth) - 1)]
        self._call += 1
        return _price_labels(count)


def test_wait_for_stable_prices_polls_until_count_stops_growing():
    page = FakePricePage(growth=[2, 5, 8, 8])
    result = asyncio.run(_wait_for_stable_prices(page))
    assert len(result.split("aria-label")) - 1 == 8
    assert page._call == 4


def test_wait_for_stable_prices_returns_immediately_if_already_stable():
    page = FakePricePage(growth=[6, 6])
    result = asyncio.run(_wait_for_stable_prices(page))
    assert len(result.split("aria-label")) - 1 == 6
    assert page._call == 2
