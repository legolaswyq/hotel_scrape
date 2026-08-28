"""Local JSON-file cache of full search results for a query, resumable
across pagination pages. Stores full Hotel objects (price/url/code) -- the
listing this feeds is also what prepay checking (marriott_prepay.py) reads
its hotel list from, so it carries the code field checking keys off.

One JSON file per distinct query lives under DATA_DIR, outside the repo
(gitignored) -- this is a cache file, not something to version.
"""

import json
from pathlib import Path
from typing import NamedTuple

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import query_key

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "search_result_cache"


class ListingProgress(NamedTuple):
    hotels: list[Hotel]
    pages_fetched: int
    complete: bool


def _path(req: SearchRequest) -> Path:
    return DATA_DIR / f"{query_key.key(req)}.json"


def load(req: SearchRequest) -> ListingProgress:
    """Return cached progress for this query. All-empty/incomplete if never cached."""
    path = _path(req)
    if not path.exists():
        return ListingProgress(hotels=[], pages_fetched=0, complete=False)
    data = json.loads(path.read_text())
    return ListingProgress(
        hotels=[Hotel(**h) for h in data.get("hotels", [])],
        pages_fetched=data.get("pages_fetched", 0),
        complete=data.get("complete", False),
    )


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
