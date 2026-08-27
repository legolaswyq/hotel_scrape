from backend.app.scraper.parsing import parse_price


def test_parses_simple_dollar_amount():
    assert parse_price("$259") == 259.0


def test_parses_amount_with_thousands_separator():
    assert parse_price("$1,234.50") == 1234.50


def test_parses_amount_with_currency_prefix():
    assert parse_price("US$259.00") == 259.0


def test_returns_none_for_missing_text():
    assert parse_price(None) is None


def test_returns_none_for_text_without_digits():
    assert parse_price("Sold Out") is None
