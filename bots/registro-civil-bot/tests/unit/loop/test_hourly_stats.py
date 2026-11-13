"""HourlyStats: в БД идёт только итог часа; закрытый час сохраняется фоном, текущий — при flush."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from tests.fakes import ManualClock, MemoryStatsRepository, settle

from rcbot.application.background import BackgroundTasks
from rcbot.application.hourly_stats import HourlyStats
from rcbot.domain.models import PollStatsHour


def make() -> tuple[HourlyStats, ManualClock, MemoryStatsRepository, BackgroundTasks]:
    clock = ManualClock()
    repository = MemoryStatsRepository()
    background = BackgroundTasks("тест")
    return HourlyStats(clock, repository, background), clock, repository, background


async def test_счётчики_одного_часа_копятся_без_записи_в_бд() -> None:
    stats, _, repository, _ = make()
    stats.count(latency_ms=10.0)
    stats.count(latency_ms=30.0)
    stats.count(error=True)
    stats.count(relogins=2)
    await settle()
    assert stats.current is not None
    assert (stats.current.checks, stats.current.errors, stats.current.relogins) == (2, 1, 2)
    assert stats.current.latencies_ms == [10.0, 30.0]
    assert not repository.saved  # каждая проверка в БД не пишется (SPEC 2.7)


async def test_смена_часа_сохраняет_прошлый_час_фоном_и_начинает_новый() -> None:
    stats, clock, repository, background = make()
    stats.count(latency_ms=10.0)
    first_hour = stats.current
    await clock.advance(3600)
    stats.count(latency_ms=20.0)
    await background.drain()
    assert list(repository.saved.values()) == [first_hour]
    assert stats.current is not None and stats.current is not first_hour
    assert stats.current.hour == first_hour.hour + timedelta(hours=1)  # type: ignore[union-attr]
    assert stats.current.latencies_ms == [20.0]


async def test_flush_сохраняет_текущий_час_и_без_данных_ничего_не_пишет() -> None:
    stats, _, repository, _ = make()
    await stats.flush()
    assert not repository.saved
    stats.count(latency_ms=5.0)
    await stats.flush()
    assert [h.checks for h in repository.saved.values()] == [1]


async def test_сбой_записи_при_flush_не_задерживает_остановку(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken:
        async def save_hour(self, hour: PollStatsHour) -> None:
            raise OSError("диск")

    clock = ManualClock()
    stats = HourlyStats(clock, Broken(), BackgroundTasks("тест"))  # type: ignore[arg-type]
    stats.count(latency_ms=1.0)
    with caplog.at_level(logging.WARNING):
        await stats.flush()
    assert "статистика часа не сохранена: OSError" in caplog.text
