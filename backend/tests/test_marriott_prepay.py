from backend.app.scraper.marriott_prepay import _extract_prepay_offer


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


def _room_heading(room_name: str) -> str:
    return f'<h3 class="standard room-name">{room_name}</h3>'


def test_extracts_member_rate_price_from_prepay_block():
    page_html = (
        "<div>Flexible Rate</div>"
        "<div>Prepay Non-refundable</div>"
        f"<div>{_rate_card('Non-Member Rate', '570', '494')}</div>"
        f"<div>{_rate_card('Member Rate', '541', '469')}</div>"
    )
    assert _extract_prepay_offer(page_html) == (541.0, None)


def test_extracts_member_rate_price_regardless_of_which_span_is_hidden():
    """The `d-none` class can land on either the tax-inclusive or base price
    span depending on page state -- confirmed live. Extraction must not
    assume a fixed order."""
    page_html = (
        "<div>Prepay Non-refundable</div>"
        f"<div>{_rate_card('Member Rate', '541', '469', tax_inclusive_first=False)}</div>"
    )
    assert _extract_prepay_offer(page_html) == (541.0, None)


def test_returns_none_when_no_prepay_plan_present():
    page_html = f"<div>Flexible Rate</div><div>{_rate_card('Member Rate', '522', '549')}</div>"
    assert _extract_prepay_offer(page_html) is None


def test_returns_none_when_prepay_present_but_member_rate_missing():
    page_html = (
        "<div>Prepay Non-refundable</div>"
        f"<div>{_rate_card('Non-Member Rate', '570', '494')}</div>"
    )
    assert _extract_prepay_offer(page_html) is None


def test_extracts_room_type_from_nearest_preceding_heading():
    """Each room type's rate cards sit under its own room-name heading --
    the prepay offer's room type is whichever heading comes right before
    it, not the first or last heading on the whole page."""
    page_html = (
        f"<div>{_room_heading('Guest room, 1 King, Low floor')}</div>"
        f"<div>{_rate_card('Member Rate', '500', '440')}</div>"
        "<div>Prepay Non-refundable</div>"
        f"<div>{_rate_card('Member Rate', '541', '469')}</div>"
        f"<div>{_room_heading('Larger Guest room, 1 King, Sofa bed')}</div>"
        f"<div>{_rate_card('Member Rate', '600', '520')}</div>"
    )
    assert _extract_prepay_offer(page_html) == (541.0, "Guest room, 1 King, Low floor")


def test_room_type_is_none_when_no_heading_precedes_the_offer():
    page_html = "<div>Prepay Non-refundable</div>" f"<div>{_rate_card('Member Rate', '541', '469')}</div>"
    assert _extract_prepay_offer(page_html) == (541.0, None)
