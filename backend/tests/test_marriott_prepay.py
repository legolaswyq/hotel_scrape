from backend.app.scraper.marriott_prepay import _build_rates_url, _extract_prepay_member_price
from backend.app.models import SearchRequest
from datetime import date


def _rate_card(rate_name: str, tax_inclusive: str, base: str) -> str:
    return (
        f'<span class="rate-name">{rate_name}</span>'
        f'<a href="/rate-details">details</a>'
        f'<span class="price"><span aria-hidden="false" class="d-none">{tax_inclusive}</span>'
        f'<span aria-hidden="false" class="">{base}</span></span>'
    )


def test_extracts_member_rate_price_from_prepay_block():
    page_html = (
        "<div>Flexible Rate</div>"
        "<div>Prepay Non-refundable</div>"
        f"<div>{_rate_card('Non-Member Rate', '570', '494')}</div>"
        f"<div>{_rate_card('Member Rate', '541', '469')}</div>"
    )
    assert _extract_prepay_member_price(page_html) == 541.0


def test_returns_none_when_no_prepay_plan_present():
    page_html = f"<div>Flexible Rate</div><div>{_rate_card('Member Rate', '522', '549')}</div>"
    assert _extract_prepay_member_price(page_html) is None


def test_returns_none_when_prepay_present_but_member_rate_missing():
    page_html = (
        "<div>Prepay Non-refundable</div>"
        f"<div>{_rate_card('Non-Member Rate', '570', '494')}</div>"
    )
    assert _extract_prepay_member_price(page_html) is None


def test_build_rates_url_includes_property_code_and_dates():
    req = SearchRequest(
        location="New York, NY",
        check_in=date(2026, 9, 11),
        check_out=date(2026, 9, 13),
        adults=1,
        rooms=1,
    )
    url = _build_rates_url(req, "NYCES")
    assert "propertyCode=NYCES" in url
    assert "fromDate=09%2F11%2F2026" in url
    assert "toDate=09%2F13%2F2026" in url
    assert "roomCount=1" in url
    assert "numAdultsPerRoom=1" in url
