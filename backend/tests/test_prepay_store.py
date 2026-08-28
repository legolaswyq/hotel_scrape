from datetime import date

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import prepay_store

REQ = SearchRequest(
    location="New York, NY",
    check_in=date(2026, 9, 11),
    check_out=date(2026, 9, 13),
    adults=1,
    rooms=1,
)


def test_load_returns_empty_when_no_prior_state(tmp_path, monkeypatch):
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path)
    checked_codes, results = prepay_store.load(REQ)
    assert checked_codes == set()
    assert results == []


def test_save_then_load_roundtrips_checked_codes_and_results(tmp_path, monkeypatch):
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path)
    hotel = Hotel(name="Courtyard", price_per_night=541.0, total_price=1082.0, currency="USD")
    prepay_store.save(REQ, {"NYCES", "NYCAK"}, [hotel])

    checked_codes, results = prepay_store.load(REQ)
    assert checked_codes == {"NYCES", "NYCAK"}
    assert results == [hotel]


def test_different_queries_use_separate_files(tmp_path, monkeypatch):
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path)
    other_req = REQ.model_copy(update={"location": "Chicago, IL"})

    prepay_store.save(REQ, {"NYCES"}, [])
    prepay_store.save(other_req, {"CHIXX"}, [])

    ny_checked, _ = prepay_store.load(REQ)
    chi_checked, _ = prepay_store.load(other_req)
    assert ny_checked == {"NYCES"}
    assert chi_checked == {"CHIXX"}


def test_save_overwrites_prior_state_for_same_query(tmp_path, monkeypatch):
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path)
    prepay_store.save(REQ, {"NYCES"}, [])
    prepay_store.save(REQ, {"NYCES", "NYCAK"}, [])

    checked_codes, _ = prepay_store.load(REQ)
    assert checked_codes == {"NYCES", "NYCAK"}
