from datetime import date

from backend.app.models import SearchRequest
from backend.app.scraper.marriott import _build_rates_url, _extract_hotels

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
    assert hotels[0].name == "Courtyard Test"
    assert hotels[0].url is not None
    assert "propertyCode=NYCES" in hotels[0].url


def test_extract_hotels_url_is_none_without_marshacode():
    page_html = (
        'class=" property-card" data-property="{&quot;hotelName&quot;:&quot;No Code Hotel&quot;}">'
        '<span aria-hidden="false" aria-label="  now  100  ">100</span>'
    )
    hotels = _extract_hotels(page_html, REQ, nights=2)
    assert len(hotels) == 1
    assert hotels[0].url is None
