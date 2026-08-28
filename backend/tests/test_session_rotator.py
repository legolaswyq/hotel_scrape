import asyncio

import pytest
from patchright.async_api import Error as PatchrightError

from backend.app.scraper.exceptions import ScraperBlockedError, ScraperInterruptedError
from backend.app.scraper.marriott import MAX_PROFILE_ROTATIONS, SessionRotator


class FakeContext:
    def __init__(self, raise_on_close: bool = False):
        self.pages = []
        self.closed = False
        self._raise_on_close = raise_on_close

    async def new_page(self):
        return object()

    async def close(self):
        if self._raise_on_close:
            raise PatchrightError("Target page, context or browser has been closed")
        self.closed = True


class FakeChromium:
    def __init__(self, contexts_raise_on_close: bool = False):
        self.launched_profile_dirs: list[str] = []
        self._contexts_raise_on_close = contexts_raise_on_close

    async def launch_persistent_context(self, profile_dir, **kwargs):
        self.launched_profile_dirs.append(profile_dir)
        return FakeContext(raise_on_close=self._contexts_raise_on_close)


class FakePlaywright:
    def __init__(self, contexts_raise_on_close: bool = False):
        self.chromium = FakeChromium(contexts_raise_on_close=contexts_raise_on_close)


def test_run_returns_result_without_rotating_on_success():
    playwright = FakePlaywright()

    async def scenario():
        async with SessionRotator(playwright) as session:
            return await session.run(lambda page: _immediate_result("ok"))

    result = asyncio.run(scenario())
    assert result == "ok"
    assert len(playwright.chromium.launched_profile_dirs) == 1


def test_no_base_profile_dir_starts_on_a_fresh_random_profile_each_time():
    """Without base_profile_dir, two separate SessionRotators must not
    start on the same profile -- there's no fixed pool to draw a shared
    starting point from (needed so concurrent sessions never collide)."""
    playwright = FakePlaywright()

    async def scenario():
        async with SessionRotator(playwright):
            pass
        async with SessionRotator(playwright):
            pass

    asyncio.run(scenario())
    dirs = playwright.chromium.launched_profile_dirs
    assert len(dirs) == 2
    assert dirs[0] != dirs[1]


def test_base_profile_dir_is_used_for_the_first_launch():
    playwright = FakePlaywright()

    async def scenario():
        async with SessionRotator(playwright, base_profile_dir="/tmp/my-stable-profile"):
            pass

    asyncio.run(scenario())
    assert playwright.chromium.launched_profile_dirs == ["/tmp/my-stable-profile"]


def test_run_rotates_to_a_fresh_random_profile_and_retries_on_block_then_succeeds():
    playwright = FakePlaywright()
    attempts = {"count": 0}

    async def flaky(page):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ScraperBlockedError("blocked")
        return "recovered"

    async def scenario():
        async with SessionRotator(playwright, base_profile_dir="/tmp/my-stable-profile") as session:
            return await session.run(flaky)

    result = asyncio.run(scenario())
    assert result == "recovered"
    assert attempts["count"] == 3
    dirs = playwright.chromium.launched_profile_dirs
    assert len(dirs) == 3
    assert len(set(dirs)) == 3
    # The stable base profile was only used for the first attempt --
    # rotations after a block are fresh, never-before-used profiles.
    assert dirs[0] == "/tmp/my-stable-profile"
    assert dirs[1] != "/tmp/my-stable-profile"
    assert dirs[2] != "/tmp/my-stable-profile"


def test_run_raises_after_exhausting_max_rotations():
    playwright = FakePlaywright()

    async def always_blocked(page):
        raise ScraperBlockedError("blocked")

    async def scenario():
        async with SessionRotator(playwright) as session:
            return await session.run(always_blocked)

    with pytest.raises(ScraperBlockedError):
        asyncio.run(scenario())

    assert len(playwright.chromium.launched_profile_dirs) == MAX_PROFILE_ROTATIONS


def test_run_raises_interrupted_without_rotating_when_browser_closed():
    playwright = FakePlaywright()

    async def closed_mid_scrape(page):
        raise PatchrightError("Target page, context or browser has been closed")

    async def scenario():
        async with SessionRotator(playwright) as session:
            return await session.run(closed_mid_scrape)

    with pytest.raises(ScraperInterruptedError):
        asyncio.run(scenario())

    # Not retried/rotated -- only the one profile from __aenter__ was launched.
    assert len(playwright.chromium.launched_profile_dirs) == 1


def test_cleanup_does_not_raise_when_context_already_closed():
    playwright = FakePlaywright(contexts_raise_on_close=True)

    async def scenario():
        async with SessionRotator(playwright) as session:
            return await session.run(lambda page: _immediate_result("ok"))

    result = asyncio.run(scenario())
    assert result == "ok"


async def _immediate_result(value):
    return value
