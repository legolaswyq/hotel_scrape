from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.models import Hotel
from backend.app.scraper.exceptions import ScraperBlockedError

client = TestClient(app)

VALID_PAYLOAD = {
    "location": "New York, NY",
    "check_in": "2026-09-11",
    "check_out": "2026-09-13",
    "adults": 1,
    "rooms": 1,
}


@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_success(mock_search):
    mock_search.return_value = [
        Hotel(name="Test Hotel", price_per_night=100.0, total_price=200.0, currency="USD")
    ]
    response = client.post("/api/search", json=VALID_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {
        "hotels": [
            {
                "name": "Test Hotel",
                "price_per_night": 100.0,
                "total_price": 200.0,
                "currency": "USD",
            }
        ]
    }


@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_blocked_returns_502(mock_search):
    mock_search.side_effect = ScraperBlockedError("blocked")
    response = client.post("/api/search", json=VALID_PAYLOAD)
    assert response.status_code == 502
    assert "error" in response.json()


def test_search_invalid_dates_returns_422():
    bad_payload = dict(VALID_PAYLOAD, check_in="2026-09-13", check_out="2026-09-11")
    response = client.post("/api/search", json=bad_payload)
    assert response.status_code == 422


@patch("backend.app.api.search_prepay", new_callable=AsyncMock)
def test_search_prepay_success(mock_search_prepay):
    mock_search_prepay.return_value = [
        Hotel(name="Prepay Hotel", price_per_night=541.0, total_price=1082.0, currency="USD")
    ]
    response = client.post("/api/search-prepay", json=VALID_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {
        "hotels": [
            {
                "name": "Prepay Hotel",
                "price_per_night": 541.0,
                "total_price": 1082.0,
                "currency": "USD",
            }
        ]
    }
    mock_search_prepay.assert_awaited_once()
    _, kwargs = mock_search_prepay.call_args
    assert kwargs["limit"] is None


@patch("backend.app.api.search_prepay", new_callable=AsyncMock)
def test_search_prepay_passes_limit_query_param(mock_search_prepay):
    mock_search_prepay.return_value = []
    response = client.post("/api/search-prepay?limit=4", json=VALID_PAYLOAD)
    assert response.status_code == 200
    _, kwargs = mock_search_prepay.call_args
    assert kwargs["limit"] == 4


@patch("backend.app.api.search_prepay", new_callable=AsyncMock)
def test_search_prepay_blocked_returns_502(mock_search_prepay):
    mock_search_prepay.side_effect = ScraperBlockedError("blocked")
    response = client.post("/api/search-prepay", json=VALID_PAYLOAD)
    assert response.status_code == 502
    assert "error" in response.json()
