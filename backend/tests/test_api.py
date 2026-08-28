from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.models import Hotel
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperInterruptedError

client = TestClient(app)

VALID_PAYLOAD = {
    "location": "New York, NY",
    "check_in": "2026-09-11",
    "check_out": "2026-09-13",
    "adults": 1,
    "rooms": 1,
}


@patch("backend.app.api.check_prepay", new_callable=AsyncMock)
@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_success(mock_search, mock_check_prepay):
    hotel = Hotel(
        name="Test Hotel",
        price_per_night=100.0,
        total_price=200.0,
        currency="USD",
        url="https://www.marriott.com/search/availabilityCalendar.mi?propertyCode=TEST",
        code="TEST",
        supports_prepay=True,
    )
    mock_search.return_value = [hotel]
    mock_check_prepay.return_value = [hotel]

    response = client.post("/api/search", json=VALID_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "hotels": [
            {
                "name": "Test Hotel",
                "price_per_night": 100.0,
                "total_price": 200.0,
                "currency": "USD",
                "url": "https://www.marriott.com/search/availabilityCalendar.mi?propertyCode=TEST",
                "code": "TEST",
                "supports_prepay": True,
            }
        ]
    }
    mock_check_prepay.assert_awaited_once()
    _, kwargs = mock_check_prepay.call_args
    assert kwargs["limit"] is None


@patch("backend.app.api.check_prepay", new_callable=AsyncMock)
@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_passes_prepay_limit_query_param(mock_search, mock_check_prepay):
    mock_search.return_value = []
    mock_check_prepay.return_value = []

    response = client.post("/api/search?prepay_limit=4", json=VALID_PAYLOAD)

    assert response.status_code == 200
    _, kwargs = mock_check_prepay.call_args
    assert kwargs["limit"] == 4


@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_blocked_returns_502(mock_search):
    mock_search.side_effect = ScraperBlockedError("blocked")
    response = client.post("/api/search", json=VALID_PAYLOAD)
    assert response.status_code == 502
    assert "error" in response.json()


@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_interrupted_returns_502(mock_search):
    mock_search.side_effect = ScraperInterruptedError("Browser session ended unexpectedly")
    response = client.post("/api/search", json=VALID_PAYLOAD)
    assert response.status_code == 502
    assert "error" in response.json()


def test_search_invalid_dates_returns_422():
    bad_payload = dict(VALID_PAYLOAD, check_in="2026-09-13", check_out="2026-09-11")
    response = client.post("/api/search", json=bad_payload)
    assert response.status_code == 422


@patch("backend.app.api.check_prepay", new_callable=AsyncMock)
@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_returns_listing_when_prepay_check_is_blocked(mock_search, mock_check_prepay):
    """If listing succeeds but the prepay-check phase gets blocked, the
    already-found hotel list must still come back instead of the whole
    request failing -- there's something to show even if some hotels'
    supports_prepay stays unknown."""
    hotel = Hotel(name="Test Hotel", price_per_night=100.0, total_price=200.0, currency="USD", code="TEST")
    mock_search.return_value = [hotel]
    mock_check_prepay.side_effect = ScraperBlockedError("blocked")

    response = client.post("/api/search", json=VALID_PAYLOAD)

    assert response.status_code == 200
    assert response.json()["hotels"][0]["name"] == "Test Hotel"
