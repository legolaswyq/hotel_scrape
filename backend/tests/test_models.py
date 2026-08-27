from datetime import date

import pytest
from pydantic import ValidationError

from backend.app.models import ErrorResponse, Hotel, SearchRequest, SearchResponse


def test_valid_search_request():
    req = SearchRequest(
        location="New York, NY",
        check_in=date(2026, 9, 11),
        check_out=date(2026, 9, 13),
        adults=1,
        rooms=1,
    )
    assert req.location == "New York, NY"


def test_rejects_checkout_before_checkin():
    with pytest.raises(ValidationError):
        SearchRequest(
            location="New York, NY",
            check_in=date(2026, 9, 13),
            check_out=date(2026, 9, 11),
            adults=1,
            rooms=1,
        )


def test_rejects_zero_adults():
    with pytest.raises(ValidationError):
        SearchRequest(
            location="New York, NY",
            check_in=date(2026, 9, 11),
            check_out=date(2026, 9, 13),
            adults=0,
            rooms=1,
        )


def test_hotel_and_response_roundtrip():
    hotel = Hotel(name="Test Hotel", price_per_night=100.0, total_price=200.0, currency="USD")
    response = SearchResponse(hotels=[hotel])
    assert response.hotels[0].name == "Test Hotel"


def test_error_response():
    err = ErrorResponse(error="blocked")
    assert err.error == "blocked"
