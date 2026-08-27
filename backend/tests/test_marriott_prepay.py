from backend.app.scraper.marriott_prepay import _extract_prepay_member_price


def _rate_card(rate_name: str, tax_inclusive: str, base: str, tax_inclusive_first: bool = True) -> str:
    spans = (
        [(tax_inclusive, "d-none"), (base, "")]
        if tax_inclusive_first
        else [(base, ""), (tax_inclusive, "d-none")]
    )
    price_spans = "".join(f'<span aria-hidden="false" class="{cls}">{val}</span>' for val, cls in spans)
    return (
        f'<span class="rate-name">{rate_name}</span>'
        f'<a href="/rate-details">details</a>'
        f'<span class="price">{price_spans}</span>'
    )


def test_extracts_member_rate_price_from_prepay_block():
    page_html = (
        "<div>Flexible Rate</div>"
        "<div>Prepay Non-refundable</div>"
        f"<div>{_rate_card('Non-Member Rate', '570', '494')}</div>"
        f"<div>{_rate_card('Member Rate', '541', '469')}</div>"
    )
    assert _extract_prepay_member_price(page_html) == 541.0


def test_extracts_member_rate_price_regardless_of_which_span_is_hidden():
    """The `d-none` class can land on either the tax-inclusive or base price
    span depending on page state -- confirmed live. Extraction must not
    assume a fixed order."""
    page_html = (
        "<div>Prepay Non-refundable</div>"
        f"<div>{_rate_card('Member Rate', '541', '469', tax_inclusive_first=False)}</div>"
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
