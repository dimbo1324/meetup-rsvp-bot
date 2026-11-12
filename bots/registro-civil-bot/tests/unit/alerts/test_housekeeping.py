"""Очистка по срокам SPEC 2.7: сроки, tmp при старте, VACUUM раз в 7 дней, квота журнала."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from tests.fakes import T0, ManualClock, MemoryKV, RecordingJournal

from rcbot.application import housekeeping
from rcbot.application.housekeeping import (
    COMPACT_EVERY,
    EVERY,
    JOURNAL_QUOTA_EVERY_SEC,
    K_LAST_COMPACT,
    Housekeeping,
)

DAY = timedelta(days=1)


class FakeMaintenance:
    def __init__(self, removed: dict[str, int] | None = None) -> None:
        self.removed = removed or {}
        self.cutoffs: list[dict[str, datetime]] = []
        self.compactions = 0
        self.compact_error: Exception | None = None

    async def delete_older_than(self, cutoffs: dict[str, datetime]) -> dict[str, int]:
        self.cutoffs.append(dict(cutoffs))
        return dict(self.removed)

    async def compact(self) -> None:
        if self.compact_error is not None:
            raise self.compact_error
        self.compactions += 1


class FakeFiles:
    def __init__(self, pruned: dict[str, int] | None = None) -> None:
        self.pruned = pruned or {}
        self.prune_calls: list[tuple[str, timedelta, datetime, int | None]] = []
        self.log_calls: list[tuple[timedelta, datetime]] = []
        self.cleared: list[str] = []
        self.clear_result = 0
        self.size = 123

    def prune(
        self, name: str, max_age: timedelta, now: datetime, max_files: int | None = None
    ) -> int:
        self.prune_calls.append((name, max_age, now, max_files))
        return self.pruned.get(name, 0)

    def clear(self, name: str) -> int:
        self.cleared.append(name)
        return self.clear_result

    def prune_logs(self, max_age: timedelta, now: datetime) -> int:
        self.log_calls.append((max_age, now))
        return self.pruned.get("logs", 0)

    def folder_size(self) -> int:
        return self.size


def _make(
    *, removed: dict[str, int] | None = None, pruned: dict[str, int] | None = None
) -> tuple[Housekeeping, FakeMaintenance, FakeFiles, MemoryKV, ManualClock, RecordingJournal]:
    clock = ManualClock()
    maintenance, files, kv, journal = (
        FakeMaintenance(removed),
        FakeFiles(pruned),
        MemoryKV(),
        (RecordingJournal()),
    )
    return (
        Housekeeping(maintenance, files, kv, clock, journal),
        maintenance,
        files,
        kv,
        clock,
        journal,
    )


async def until(condition: Callable[[], bool], limit: float = 3.0) -> None:
    """Дождаться условия, которое выполняют потоки (`to_thread`): ручные часы им не помогают."""
    deadline = asyncio.get_running_loop().time() + limit
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "условие не наступило"
        await asyncio.sleep(0.002)


async def cycle(clock: ManualClock, seconds: float, done: Callable[[], bool]) -> None:
    """Сдвинуть часы и дождаться, пока цикл отработает и снова заснёт: иначе следующий сдвиг
    пройдёт «мимо» ещё не заснувшей задачи и потеряет период."""
    await clock.advance(seconds)
    await until(done)
    await until(lambda: clock.sleeping == 1)


# --- сроки хранения ---


async def test_сроки_хранения_в_бд_соответствуют_spec_2_7() -> None:
    hk, maintenance, *_ = _make()
    await hk.run_once()
    [cutoffs] = maintenance.cutoffs
    assert cutoffs == {
        "incidents": T0 - 180 * DAY,
        "poll_stats_hourly": T0 - 30 * DAY,
        "app_events": T0 - 30 * DAY,
    }


async def test_сроки_и_лимиты_файлов_соответствуют_spec_2_7() -> None:
    hk, _, files, *_ = _make()
    await hk.run_once()
    assert files.prune_calls == [
        ("discovery", 7 * DAY, T0, None),
        ("quarantine", 7 * DAY, T0, 20),
        ("shots", 3 * DAY, T0, 20),
    ]
    assert files.log_calls == [(14 * DAY, T0)]


async def test_выполненная_очистка_возвращает_только_ненулевое_и_пишет_в_журнал() -> None:
    hk, _, _, _, _, journal = _make(
        removed={"incidents": 2, "app_events": 0}, pruned={"quarantine": 5, "logs": 1}
    )
    removed = await hk.run_once()
    assert removed == {"incidents": 2, "quarantine": 5, "logs": 1}
    [(_, message, level, _)] = [e for e in journal.events if e[0] == "housekeeping"]
    assert level == "INFO" and "quarantine" in message


async def test_если_удалять_нечего_запись_в_журнале_не_делается() -> None:
    hk, *_, journal = _make()
    assert await hk.run_once() == {}
    assert journal.codes() == []


async def test_сроки_считаются_от_текущего_момента_часов_на_каждом_запуске() -> None:
    hk, maintenance, files, _, clock, _ = _make()
    await hk.run_once()
    await clock.advance(6 * 3600)
    await hk.run_once()
    assert maintenance.cutoffs[1]["incidents"] == T0 + timedelta(hours=6) - 180 * DAY
    assert files.prune_calls[-1][2] == T0 + timedelta(hours=6)


def test_временные_файлы_очищаются_при_старте() -> None:
    hk, _, files, *_ = _make()
    files.clear_result = 4
    assert hk.start() == 4
    assert files.cleared == ["tmp"]


def test_размер_папки_берётся_из_хранилища_файлов() -> None:
    hk, _, files, *_ = _make()
    files.size = 42
    assert hk.folder_size() == 42


# --- VACUUM раз в 7 дней ---


async def test_первый_запуск_уплотняет_бд_и_запоминает_момент() -> None:
    hk, maintenance, _, kv, _, _ = _make()
    await hk.run_once()
    assert maintenance.compactions == 1
    assert kv.data[K_LAST_COMPACT] == T0.isoformat()


async def test_уплотнение_не_чаще_раза_в_7_дней_и_ровно_через_7() -> None:
    hk, maintenance, _, _, clock, _ = _make()
    await hk.run_once()
    await clock.advance(COMPACT_EVERY.total_seconds() - 1)
    await hk.run_once()
    assert maintenance.compactions == 1  # без секунды до недели — рано
    await clock.advance(1)
    await hk.run_once()
    assert maintenance.compactions == 2  # ровно через неделю — пора
    await clock.advance(3 * 3600)
    await hk.run_once()
    assert maintenance.compactions == 2


async def test_частые_запуски_уплотняют_только_раз_в_неделю() -> None:
    hk, maintenance, *_, clock, _ = _make()
    for _ in range(30):  # 30 запусков по 6 часов = 7,25 суток
        await hk.run_once()
        await clock.advance(EVERY.total_seconds())
    assert maintenance.compactions == 2


async def test_сбой_уплотнения_не_запоминается_и_повторится_в_следующий_раз() -> None:
    hk, maintenance, _, kv, _, _ = _make()
    maintenance.compact_error = OSError("файл БД занят")
    with pytest.raises(OSError):
        await hk.run_once()
    assert K_LAST_COMPACT not in kv.data
    maintenance.compact_error = None
    await hk.run_once()
    assert maintenance.compactions == 1


@pytest.mark.parametrize(
    "garbage",
    ["мусор", "", "2026-13-45", "2026-11-12T15:00:00", "0", "None"],
    ids=["текст", "пусто", "невозможная дата", "naive-дата", "ноль", "None"],
)
async def test_повреждённый_момент_последнего_уплотнения_не_роняет_очистку(garbage: str) -> None:
    hk, maintenance, _, kv, _, _ = _make()
    await kv.set(K_LAST_COMPACT, garbage)
    await hk.run_once()  # раньше: ValueError/TypeError, и так на каждом запуске
    assert maintenance.compactions == 1
    assert kv.data[K_LAST_COMPACT] == T0.isoformat()


async def test_момент_уплотнения_из_будущего_не_блокирует_его_навсегда() -> None:
    hk, maintenance, _, kv, _, _ = _make()
    await kv.set(K_LAST_COMPACT, (T0 + 400 * DAY).isoformat())  # часы переводили назад
    await hk.run_once()
    assert maintenance.compactions == 1


# --- циклы ---


async def test_run_forever_запускает_очистку_сразу_и_каждые_6_часов() -> None:
    hk, maintenance, _, _, clock, _ = _make()
    task = asyncio.create_task(hk.run_forever())
    await until(lambda: len(maintenance.cutoffs) == 1)
    await until(lambda: clock.sleeping == 1)
    await clock.advance(EVERY.total_seconds() - 1)
    assert len(maintenance.cutoffs) == 1
    await cycle(clock, 1, lambda: len(maintenance.cutoffs) == 2)
    await cycle(clock, EVERY.total_seconds(), lambda: len(maintenance.cutoffs) == 3)
    assert maintenance.cutoffs[2]["incidents"] == T0 + 2 * EVERY - 180 * DAY
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_forever_делает_vacuum_через_неделю_по_6_часовому_циклу() -> None:
    hk, maintenance, _, _, clock, _ = _make()
    task = asyncio.create_task(hk.run_forever())
    await until(lambda: maintenance.compactions == 1)
    await until(lambda: clock.sleeping == 1)
    for runs in range(2, 30):  # 28 × 6 ч = 7 суток
        await cycle(
            clock, EVERY.total_seconds(), lambda runs=runs: len(maintenance.cutoffs) == runs
        )
        assert maintenance.compactions == (2 if runs == 29 else 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_периоды_очистки_и_квоты_соответствуют_spec() -> None:
    assert timedelta(hours=6) == EVERY
    assert timedelta(days=7) == COMPACT_EVERY
    assert JOURNAL_QUOTA_EVERY_SEC == 600.0
    assert housekeeping.LOG_RETENTION == 14 * DAY


# --- квота журнала ---


async def test_квота_журнала_без_удаления_ничего_не_пишет() -> None:
    hk, *_, journal = _make()
    journal.quota_result = (0, 0)
    assert await hk.journal_quota_once() == (0, 0)
    assert journal.quota_calls == 1
    assert journal.events == []


async def test_квота_журнала_при_удалении_пишет_предупреждение_с_числом_файлов_и_гигабайтами(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hk, *_, journal = _make()
    journal.quota_result = (3, int(1.5 * 2**30))
    assert await hk.journal_quota_once() == (3, int(1.5 * 2**30))
    [(code, message, level, _)] = journal.events
    assert (code, level) == ("journal.quota", "WARNING")
    assert "3" in message and "1.50 ГБ" in message
    assert "квота журнала" in caplog.text


async def test_квота_журнала_проверяется_сразу_и_каждые_10_минут() -> None:
    hk, *_, clock, journal = _make()
    task = asyncio.create_task(hk.journal_quota_forever())
    await until(lambda: journal.quota_calls == 1)
    await clock.advance(599)
    assert journal.quota_calls == 1
    await clock.advance(1)
    await until(lambda: journal.quota_calls == 2)
    await clock.advance(1200)
    await until(lambda: journal.quota_calls == 4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_без_журнала_квота_нулевая() -> None:
    hk = Housekeeping(FakeMaintenance(), FakeFiles(), MemoryKV(), ManualClock())
    assert await hk.journal_quota_once() == (0, 0)
