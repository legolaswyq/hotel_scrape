from datetime import date

from backend.app.models import SearchRequest
from backend.app.scraper import hotel_list_store

REQ = SearchRequest(
    location="New York, NY",
    check_in=date(2026, 9, 11),
    check_out=date(2026, 9, 13),
    adults=1,
    rooms=1,
)


def test_load_returns_empty_incomplete_progress_when_no_prior_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(hotel_list_store, "DATA_DIR", tmp_path)
    progress = hotel_list_store.load(REQ)
    assert progress.hotels == []
    assert progress.pages_fetched == 0
    assert progress.complete is False


def test_save_then_load_roundtrips_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(hotel_list_store, "DATA_DIR", tmp_path)
    hotels = [("NYCES", "Courtyard"), ("NYCAK", "Algonquin")]
    hotel_list_store.save(REQ, hotels, pages_fetched=2, complete=True)

    progress = hotel_list_store.load(REQ)
    assert progress.hotels == hotels
    assert progress.pages_fetched == 2
    assert progress.complete is True


def test_different_queries_use_separate_files(tmp_path, monkeypatch):
    monkeypatch.setattr(hotel_list_store, "DATA_DIR", tmp_path)
    other_req = REQ.model_copy(update={"location": "Chicago, IL"})

    hotel_list_store.save(REQ, [("NYCES", "Courtyard")], pages_fetched=1, complete=False)
    hotel_list_store.save(other_req, [("CHIXX", "Some Chicago Hotel")], pages_fetched=1, complete=True)

    assert hotel_list_store.load(REQ).hotels == [("NYCES", "Courtyard")]
    assert hotel_list_store.load(REQ).complete is False
    assert hotel_list_store.load(other_req).hotels == [("CHIXX", "Some Chicago Hotel")]
    assert hotel_list_store.load(other_req).complete is True
