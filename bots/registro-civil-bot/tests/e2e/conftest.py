"""Фикстуры сквозных тестов: «сайт», сервер каналов, сборщик стендов, часы стенда."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from rcbot.config.paths import Paths
from tests.e2e.clock import StandClock
from tests.e2e.site import ChannelsSite, FakeSite
from tests.e2e.stand import Stand
from tests.fakes import LocalTlsServer

StandFactory = Callable[..., Awaitable[Stand]]


@pytest.fixture
def stand_clock() -> StandClock:
    return StandClock()


@pytest.fixture
async def site(stand_clock: StandClock) -> AsyncIterator[FakeSite]:
    async with LocalTlsServer() as server:
        fake = FakeSite(server, stand_clock)
        yield fake
        # Зависшие ответы отпускаются до остановки сервера, иначе он ждёт их до таймаута
        fake.release_hang.set()


@pytest.fixture
async def channels_site() -> AsyncIterator[ChannelsSite]:
    async with LocalTlsServer() as server:
        yield ChannelsSite(server)


@pytest.fixture
async def make_stand(
    paths: Paths, tmp_path: Path, stand_clock: StandClock, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[StandFactory]:
    stands: list[Stand] = []

    async def factory(**kwargs: Any) -> Stand:
        stand = await Stand.build(
            paths=paths,
            journal_root=tmp_path / "journal",
            clock=stand_clock,
            monkeypatch=monkeypatch,
            **kwargs,
        )
        stands.append(stand)
        return stand

    yield factory
    for stand in reversed(stands):
        await stand.crash()
