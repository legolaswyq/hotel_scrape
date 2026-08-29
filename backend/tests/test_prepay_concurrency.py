import asyncio
import re
from datetime import date
from unittest.mock import AsyncMock

import backend.app.scraper.marriott_prepay as marriott_prepay_module
from backend.app.models import Hotel, SearchRequest
from backend.app.scraper import prepay_store
from backend.app.scraper.marriott_prepay import PREPAY_WORKER_COUNT, check_prepay

REQ = SearchRequest(
    location="New York, NY",
    check_in=date(2026, 9, 11),
    check_out=date(2026, 9, 13),
    adults=1,
    rooms=1,
)

CODE_RE = re.compile(r"propertyCode=(\w+)")

# One hotel (H4) deliberately has no prepay marker, to check that a
# not-offered result doesn't get recorded as a "found" hotel.
PRICES = {"H1": "469", "H2": "531", "H3": "402", "H5": "615", "H6": "388"}
HOTELS = [
    Hotel(name=name, price_per_night=100.0, total_price=200.0, currency="USD", code=code)
    for code, name in [
        ("H1", "One"),
        ("H2", "Two"),
        ("H3", "Three"),
        ("H4", "Four"),
        ("H5", "Five"),
        ("H6", "Six"),
    ]
]


def _rate_html(price: str | None) -> str:
    if price is None:
        return "<div>Flexible Rate only, no prepay here</div>"
    return (
        '<h3 class="standard room-name">Guest room, 1 King</h3>'
        "<div>Prepay Non-refundable</div>"
        '<span class="rate-name">Member Rate</span>'
        '<a href="/rate-details">details</a>'
        f'<span class="price"><span aria-hidden="false" class="d-none">{price}</span>'
        f'<span aria-hidden="false" class="">{price}</span></span>'
    )


class FakePage:
    def __init__(self):
        self._last_code: str | None = None
        self.goto = self._goto
        self.wait_for_timeout = AsyncMock()
        self.title = AsyncMock(return_value="Where Can We Take You?")
        self.mouse = type("Mouse", (), {"move": AsyncMock(), "wheel": AsyncMock()})()

        click_mock = AsyncMock()
        button = type("Button", (), {"first": type("First", (), {"click": click_mock})()})()
        self.get_by_role = lambda *a, **k: button

    async def _goto(self, url, **kwargs):
        match = CODE_RE.search(url)
        self._last_code = match.group(1) if match else None
        return AsyncMock(status=200)

    async def content(self):
        return _rate_html(PRICES.get(self._last_code))


class FakeContext:
    def __init__(self, profile_dir: str):
        self.profile_dir = profile_dir
        self.pages = []

    async def new_page(self):
        return FakePage()

    async def close(self):
        pass


class FakeChromium:
    def __init__(self):
        self.launched_profile_dirs: list[str] = []

    async def launch_persistent_context(self, profile_dir, **kwargs):
        self.launched_profile_dirs.append(profile_dir)
        return FakeContext(profile_dir)


class FakePlaywrightCtx:
    def __init__(self):
        self.chromium = FakeChromium()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        pass


def test_prepay_checks_all_hotels_across_configured_worker_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path / "prepay_cache")
    monkeypatch.setattr(marriott_prepay_module, "DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(marriott_prepay_module, "DELAY_MAX_SECONDS", 0.0)

    fake_ctx = FakePlaywrightCtx()
    monkeypatch.setattr(marriott_prepay_module, "async_playwright", lambda: fake_ctx)

    hotels = [h.model_copy() for h in HOTELS]
    result = asyncio.run(check_prepay(REQ, hotels))

    result_by_code = {h.code: h for h in result}
    for code in PRICES:
        assert result_by_code[code].supports_prepay is True
        assert result_by_code[code].room_type == "Guest room, 1 King"
    assert result_by_code["H4"].supports_prepay is False

    cached_checked, cached_results = prepay_store.load(REQ)
    assert cached_checked == {"H1", "H2", "H3", "H4", "H5", "H6"}
    assert {h.code for h in cached_results} == set(PRICES)

    # PREPAY_WORKER_COUNT distinct browser sessions (profile dirs) were
    # used -- each worker launches its own, regardless of how many
    # hotels it ends up checking.
    dirs = fake_ctx.chromium.launched_profile_dirs
    assert len(dirs) == PREPAY_WORKER_COUNT
    assert len(set(dirs)) == len(dirs)


def test_always_launches_exactly_worker_count_sessions_even_with_fewer_candidates(tmp_path, monkeypatch):
    """PREPAY_WORKER_COUNT browser sessions must spin up regardless of
    batch size -- even a single hotel to check launches all of them (the
    rest just find an empty queue and exit immediately), rather than
    scaling worker count down with the batch."""
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path / "prepay_cache")
    monkeypatch.setattr(marriott_prepay_module, "DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(marriott_prepay_module, "DELAY_MAX_SECONDS", 0.0)

    fake_ctx = FakePlaywrightCtx()
    monkeypatch.setattr(marriott_prepay_module, "async_playwright", lambda: fake_ctx)

    hotels = [HOTELS[0].model_copy()]
    asyncio.run(check_prepay(REQ, hotels))

    assert len(fake_ctx.chromium.launched_profile_dirs) == PREPAY_WORKER_COUNT


def test_second_call_with_limit_only_checks_new_hotels(tmp_path, monkeypatch):
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path / "prepay_cache")
    monkeypatch.setattr(marriott_prepay_module, "DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(marriott_prepay_module, "DELAY_MAX_SECONDS", 0.0)

    fake_ctx = FakePlaywrightCtx()
    monkeypatch.setattr(marriott_prepay_module, "async_playwright", lambda: fake_ctx)

    hotels = [h.model_copy() for h in HOTELS]
    first = asyncio.run(check_prepay(REQ, hotels, limit=2))
    checked_after_first = {h.code for h in first if h.supports_prepay is not None}
    assert len(checked_after_first) == 2
    untouched = {h.code for h in first if h.supports_prepay is None}
    assert len(untouched) == 4

    second = asyncio.run(check_prepay(REQ, hotels))
    assert all(h.supports_prepay is not None for h in second)

    cached_checked, _ = prepay_store.load(REQ)
    assert cached_checked == {"H1", "H2", "H3", "H4", "H5", "H6"}


def test_stale_cache_entry_missing_room_type_gets_rechecked(tmp_path, monkeypatch):
    """A prepay result saved before Hotel gained room_type (supports_prepay
    True, room_type None) must not stay stuck that way forever just
    because its code is already in checked_codes -- it should be forced
    back into the queue and picked up on the next call."""
    monkeypatch.setattr(prepay_store, "DATA_DIR", tmp_path / "prepay_cache")
    monkeypatch.setattr(marriott_prepay_module, "DELAY_MIN_SECONDS", 0.0)
    monkeypatch.setattr(marriott_prepay_module, "DELAY_MAX_SECONDS", 0.0)

    stale_hotel = Hotel(
        name="One", price_per_night=469.0, total_price=938.0, currency="USD", code="H1", supports_prepay=True
    )
    prepay_store.save(REQ, {"H1"}, [stale_hotel])

    fake_ctx = FakePlaywrightCtx()
    monkeypatch.setattr(marriott_prepay_module, "async_playwright", lambda: fake_ctx)

    hotels = [HOTELS[0].model_copy()]
    result = asyncio.run(check_prepay(REQ, hotels))

    assert result[0].room_type == "Guest room, 1 King"
    _, cached_results = prepay_store.load(REQ)
    assert cached_results[0].room_type == "Guest room, 1 King"
