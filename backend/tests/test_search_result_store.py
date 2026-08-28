import os
from datetime import date

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import search_result_store

REQ_NYC = SearchRequest(
    location="New York, NY",
    check_in=date(2026, 9, 11),
    check_out=date(2026, 9, 13),
    adults=1,
    rooms=1,
)
REQ_CHI = SearchRequest(
    location="Chicago, IL",
    check_in=date(2026, 10, 1),
    check_out=date(2026, 10, 3),
    adults=2,
    rooms=1,
)


def test_list_all_returns_empty_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(search_result_store, "DATA_DIR", tmp_path)
    assert search_result_store.list_all() == []


def test_list_all_summarizes_each_cached_query(tmp_path, monkeypatch):
    monkeypatch.setattr(search_result_store, "DATA_DIR", tmp_path)

    search_result_store.save(
        REQ_NYC,
        [
            Hotel(name="A", price_per_night=100.0, total_price=200.0, currency="USD", code="A", supports_prepay=True),
            Hotel(name="B", price_per_night=150.0, total_price=300.0, currency="USD", code="B", supports_prepay=None),
        ],
        pages_fetched=1,
        complete=True,
    )
    search_result_store.save(
        REQ_CHI,
        [Hotel(name="C", price_per_night=90.0, total_price=180.0, currency="USD", code="C")],
        pages_fetched=1,
        complete=False,
    )

    summaries = {s.location: s for s in search_result_store.list_all()}

    assert summaries["New York, NY"].hotel_count == 2
    assert summaries["New York, NY"].prepay_checked_count == 1
    assert summaries["New York, NY"].complete is True

    assert summaries["Chicago, IL"].hotel_count == 1
    assert summaries["Chicago, IL"].prepay_checked_count == 0
    assert summaries["Chicago, IL"].complete is False
    assert summaries["Chicago, IL"].adults == 2


def test_list_all_orders_most_recently_saved_first(tmp_path, monkeypatch):
    monkeypatch.setattr(search_result_store, "DATA_DIR", tmp_path)

    search_result_store.save(REQ_NYC, [], pages_fetched=0, complete=False)
    search_result_store.save(REQ_CHI, [], pages_fetched=0, complete=False)

    # Force distinct mtimes rather than relying on real-clock resolution
    # (can be too coarse to separate two saves in the same test run).
    now = 1_800_000_000
    os.utime(search_result_store._path(REQ_CHI), (now, now))
    os.utime(search_result_store._path(REQ_NYC), (now + 10, now + 10))

    summaries = search_result_store.list_all()
    assert summaries[0].location == "New York, NY"
