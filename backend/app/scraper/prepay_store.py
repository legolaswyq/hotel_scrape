"""Local JSON-file persistence for prepay scan progress, keyed by search query.

Lets search_prepay() resume across calls: hotels already checked for a
given location/dates/guest count are remembered, so a later call with the
same query continues from the next unchecked hotel instead of starting
over and re-checking (and re-risking a block on) hotels already done.

One JSON file per distinct query lives under DATA_DIR, outside the repo
(gitignored) -- this is a cache/progress file, not something to version.
"""

import hashlib
import json
from pathlib import Path

from backend.app.models import Hotel, SearchRequest

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "prepay_cache"


def _key(req: SearchRequest) -> str:
    raw = f"{req.location.strip().lower()}|{req.check_in}|{req.check_out}|{req.adults}|{req.rooms}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _path(req: SearchRequest) -> Path:
    return DATA_DIR / f"{_key(req)}.json"


def load(req: SearchRequest) -> tuple[set[str], list[Hotel]]:
    """Return (checked_codes, results) from a prior run for this query, or empty if none."""
    path = _path(req)
    if not path.exists():
        return set(), []
    data = json.loads(path.read_text())
    checked_codes = set(data.get("checked_codes", []))
    results = [Hotel(**h) for h in data.get("results", [])]
    return checked_codes, results


def save(req: SearchRequest, checked_codes: set[str], results: list[Hotel]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "location": req.location,
        "check_in": str(req.check_in),
        "check_out": str(req.check_out),
        "adults": req.adults,
        "rooms": req.rooms,
        "checked_codes": sorted(checked_codes),
        "results": [h.model_dump() for h in results],
    }
    _path(req).write_text(json.dumps(payload, indent=2))
