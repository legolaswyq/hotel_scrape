import asyncio

import pytest

from backend.app.scraper.exceptions import ScraperBlockedError
from backend.app.scraper.marriott import PROFILE_POOL_SIZE, SessionRotator


class FakeContext:
    def __init__(self):
        self.pages = []
        self.closed = False

    async def new_page(self):
        return object()

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.launched_profile_dirs: list[str] = []

    async def launch_persistent_context(self, profile_dir, **kwargs):
        self.launched_profile_dirs.append(profile_dir)
        return FakeContext()


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


def test_run_returns_result_without_rotating_on_success():
    playwright = FakePlaywright()

    async def scenario():
        async with SessionRotator(playwright) as session:
            return await session.run(lambda page: _immediate_result("ok"))

    result = asyncio.run(scenario())
    assert result == "ok"
    assert len(playwright.chromium.launched_profile_dirs) == 1


def test_run_rotates_profile_and_retries_on_block_then_succeeds():
    playwright = FakePlaywright()
    attempts = {"count": 0}

    async def flaky(page):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ScraperBlockedError("blocked")
        return "recovered"

    async def scenario():
        async with SessionRotator(playwright) as session:
            return await session.run(flaky)

    result = asyncio.run(scenario())
    assert result == "recovered"
    assert attempts["count"] == 3
    assert len(playwright.chromium.launched_profile_dirs) == 3
    assert len(set(playwright.chromium.launched_profile_dirs)) == 3


def test_run_raises_after_exhausting_profile_pool():
    playwright = FakePlaywright()

    async def always_blocked(page):
        raise ScraperBlockedError("blocked")

    async def scenario():
        async with SessionRotator(playwright) as session:
            return await session.run(always_blocked)

    with pytest.raises(ScraperBlockedError):
        asyncio.run(scenario())

    assert len(playwright.chromium.launched_profile_dirs) == PROFILE_POOL_SIZE


async def _immediate_result(value):
    return value
