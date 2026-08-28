"""Local JSON-file cache of the full hotel list for a search query.

Marriott's search results are paginated (40 hotels per page). Fetching the
full list means clicking through every page once. This is cheap compared
to the per-hotel prepay checks (a handful of page-turns vs. one navigation
per hotel), so it's fetched once per distinct query and cached here --
later calls for the same location/dates/guest count reuse the cached list
instead of re-paginating.

One JSON file per distinct query lives under DATA_DIR, outside the repo
(gitignored) -- this is a cache file, not something to version.
"""

import json
from pathlib import Path

from backend.app.models import SearchRequest
from backend.app.scraper import query_key

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "hotel_list_cache"


def _path(req: SearchRequest) -> Path:
    return DATA_DIR / f"{query_key.key(req)}.json"


def load(req: SearchRequest) -> list[tuple[str, str]] | None:
    """Return the cached (marshacode, hotelName) list for this query, or None if not cached."""
    path = _path(req)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return [(code, name) for code, name in data.get("hotels", [])]


def save(req: SearchRequest, hotels: list[tuple[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "location": req.location,
        "check_in": str(req.check_in),
        "check_out": str(req.check_out),
        "adults": req.adults,
        "rooms": req.rooms,
        "hotels": [[code, name] for code, name in hotels],
    }
    _path(req).write_text(json.dumps(payload, indent=2))
