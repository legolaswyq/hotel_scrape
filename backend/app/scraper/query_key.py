"""Stable cache key for a search query (location/dates/guest count).

Shared by hotel_list_store.py and prepay_store.py so both cache layers
agree on what counts as "the same query".
"""

import hashlib

from backend.app.models import SearchRequest


def key(req: SearchRequest) -> str:
    raw = f"{req.location.strip().lower()}|{req.check_in}|{req.check_out}|{req.adults}|{req.rooms}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
