import re

_PRICE_RE = re.compile(r"[\d,]+\.?\d*")


def parse_price(text: str | None) -> float | None:
    """Extract a numeric price from raw display text, e.g. '$1,234.50' -> 1234.5."""
    if not text:
        return None
    match = _PRICE_RE.search(text)
    if not match:
        return None
    cleaned = match.group(0).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None
