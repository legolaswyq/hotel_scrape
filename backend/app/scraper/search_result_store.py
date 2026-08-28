"""Local JSON-file cache of full search results for a query, resumable
across pagination pages. Stores full Hotel objects (price/url/code) -- the
listing this feeds is also what prepay checking (marriott_prepay.py) reads
its hotel list from, so it carries the code field checking keys off.

One JSON file per distinct query lives under DATA_DIR, outside the repo
(gitignored) -- this is a cache file, not something to version.
"""

import json
import re
from pathlib import Path
from typing import NamedTuple

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import query_key

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "search_result_cache"


class ListingProgress(NamedTuple):
    hotels: list[Hotel]
    pages_fetched: int
    complete: bool


class SearchSummary(NamedTuple):
    key: str
    location: str
    check_in: str
    check_out: str
    adults: int
    rooms: int
    hotel_count: int
    prepay_checked_count: int
    complete: bool
    searched_at: float


# Cache files written before Hotel gained a `code` field have `code: null`
# on every hotel -- which silently makes check_prepay() skip all of them
# (it can't check a hotel it can't build a rate URL for) even though the
# code is recoverable from the `propertyCode=` query param already saved
# in each hotel's `url`. Backfill it on load instead of forcing a full
# re-scrape of an otherwise-valid cache.
_PROPERTY_CODE_RE = re.compile(r"propertyCode=([^&]+)")


def _backfill_code(hotel: Hotel) -> Hotel:
    if hotel.code or not hotel.url:
        return hotel
    match = _PROPERTY_CODE_RE.search(hotel.url)
    if not match:
        return hotel
    return hotel.model_copy(update={"code": match.group(1)})


def _path(req: SearchRequest) -> Path:
    return DATA_DIR / f"{query_key.key(req)}.json"


def _progress_from_json(data: dict) -> ListingProgress:
    return ListingProgress(
        hotels=[_backfill_code(Hotel(**h)) for h in data.get("hotels", [])],
        pages_fetched=data.get("pages_fetched", 0),
        complete=data.get("complete", False),
    )


def load(req: SearchRequest) -> ListingProgress:
    """Return cached progress for this query. All-empty/incomplete if never cached."""
    path = _path(req)
    if not path.exists():
        return ListingProgress(hotels=[], pages_fetched=0, complete=False)
    return _progress_from_json(json.loads(path.read_text()))


_KEY_RE = re.compile(r"^[0-9a-f]{16}$")


def load_by_key(key: str) -> ListingProgress | None:
    """Same as load(), but by the cache key list_all() reports directly --
    for showing a past search's results without needing to reconstruct a
    SearchRequest (or re-trigger a scrape) for it. None if the key doesn't
    correspond to a cache file (e.g. it was cleared since list_all() ran)
    or isn't a well-formed key (query_key.key()'s format -- rejected
    up front since this value comes from the request path and a
    malformed one could otherwise be used to escape DATA_DIR)."""
    if not _KEY_RE.match(key):
        return None
    path = DATA_DIR / f"{key}.json"
    if not path.exists():
        return None
    return _progress_from_json(json.loads(path.read_text()))


def list_all() -> list[SearchSummary]:
    """Return a summary of every cached search, most recently searched
    first (by file modification time -- both save() and re-runs of the
    same query touch the file, so this tracks "last searched", not just
    "first searched")."""
    if not DATA_DIR.exists():
        return []
    summaries = []
    for path in DATA_DIR.glob("*.json"):
        data = json.loads(path.read_text())
        hotels = data.get("hotels", [])
        summaries.append(
            SearchSummary(
                key=path.stem,
                location=data.get("location", ""),
                check_in=data.get("check_in", ""),
                check_out=data.get("check_out", ""),
                adults=data.get("adults", 1),
                rooms=data.get("rooms", 1),
                hotel_count=len(hotels),
                prepay_checked_count=sum(1 for h in hotels if h.get("supports_prepay") is not None),
                complete=data.get("complete", False),
                searched_at=path.stat().st_mtime,
            )
        )
    summaries.sort(key=lambda s: s.searched_at, reverse=True)
    return summaries


def save(req: SearchRequest, hotels: list[Hotel], pages_fetched: int, complete: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "location": req.location,
        "check_in": str(req.check_in),
        "check_out": str(req.check_out),
        "adults": req.adults,
        "rooms": req.rooms,
        "hotels": [h.model_dump() for h in hotels],
        "pages_fetched": pages_fetched,
        "complete": complete,
    }
    _path(req).write_text(json.dumps(payload, indent=2))
