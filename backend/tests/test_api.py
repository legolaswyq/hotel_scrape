from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.models import Hotel
from backend.app.scraper import search_result_store
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperInterruptedError
from backend.app.scraper.search_result_store import SearchSummary

client = TestClient(app)

VALID_PAYLOAD = {
    "location": "New York, NY",
    "check_in": "2026-09-11",
    "check_out": "2026-09-13",
    "adults": 1,
    "rooms": 1,
}


@pytest.fixture(autouse=True)
def isolated_search_result_cache(tmp_path, monkeypatch):
    """/api/search now persists check_prepay's annotations back into
    search_result_store -- point every test at a throwaway cache dir so
    none of them touch (or depend on) the real one."""
    monkeypatch.setattr(search_result_store, "DATA_DIR", tmp_path)


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


@patch("backend.app.api.check_prepay", new_callable=AsyncMock)
@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_persists_prepay_annotations_for_later_history_lookup(mock_search, mock_check_prepay):
    """check_prepay() only annotates hotel.supports_prepay in memory --
    /api/search must persist that back into search_result_store so a later
    /api/search-history/{key} view (which reads the cache directly, not via
    check_prepay) reflects prepay status instead of showing it as unknown."""
    hotel = Hotel(name="Test Hotel", price_per_night=100.0, total_price=200.0, currency="USD", code="TEST")
    mock_search.return_value = [hotel]

    async def fake_check_prepay(req, hotels, limit=None):
        hotels[0].supports_prepay = True
        return hotels

    mock_check_prepay.side_effect = fake_check_prepay

    from backend.app.models import SearchRequest

    response = client.post("/api/search", json=VALID_PAYLOAD)
    assert response.status_code == 200
    assert response.json()["hotels"][0]["supports_prepay"] is True

    req = SearchRequest(**VALID_PAYLOAD)
    cached = search_result_store.load(req)
    assert cached.hotels[0].supports_prepay is True


@patch("backend.app.api.search_result_store.list_all")
def test_search_history_returns_cached_queries_most_recent_first(mock_list_all):
    mock_list_all.return_value = [
        SearchSummary(
            key="aaaa000000000001",
            location="New York, NY",
            check_in="2026-09-11",
            check_out="2026-09-13",
            adults=1,
            rooms=1,
            hotel_count=205,
            prepay_checked_count=10,
            complete=True,
            searched_at=2.0,
        ),
        SearchSummary(
            key="bbbb000000000002",
            location="Chicago, IL",
            check_in="2026-10-01",
            check_out="2026-10-03",
            adults=2,
            rooms=1,
            hotel_count=50,
            prepay_checked_count=0,
            complete=False,
            searched_at=1.0,
        ),
    ]

    response = client.get("/api/search-history")

    assert response.status_code == 200
    assert response.json() == {
        "searches": [
            {
                "key": "aaaa000000000001",
                "location": "New York, NY",
                "check_in": "2026-09-11",
                "check_out": "2026-09-13",
                "adults": 1,
                "rooms": 1,
                "hotel_count": 205,
                "prepay_checked_count": 10,
                "complete": True,
            },
            {
                "key": "bbbb000000000002",
                "location": "Chicago, IL",
                "check_in": "2026-10-01",
                "check_out": "2026-10-03",
                "adults": 2,
                "rooms": 1,
                "hotel_count": 50,
                "prepay_checked_count": 0,
                "complete": False,
            },
        ]
    }


@patch("backend.app.api.search_result_store.load_by_key")
def test_search_history_detail_returns_cached_hotels(mock_load_by_key):
    from backend.app.scraper.search_result_store import ListingProgress

    hotel = Hotel(name="Test Hotel", price_per_night=100.0, total_price=200.0, currency="USD", code="TEST")
    mock_load_by_key.return_value = ListingProgress(hotels=[hotel], pages_fetched=1, complete=True)

    response = client.get("/api/search-history/aaaa000000000001")

    assert response.status_code == 200
    assert response.json()["hotels"][0]["name"] == "Test Hotel"
    mock_load_by_key.assert_called_once_with("aaaa000000000001")


@patch("backend.app.api.search_result_store.load_by_key")
def test_search_history_detail_404s_for_unknown_key(mock_load_by_key):
    mock_load_by_key.return_value = None
    response = client.get("/api/search-history/0000000000000000")
    assert response.status_code == 404
