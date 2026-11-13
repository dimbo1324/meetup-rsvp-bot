"""Сроки хранения и очистка (SPEC 2.7): константы совпадают с таблицей SPEC и реально работают."""

from __future__ import annotations

import logging
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from rcbot.application import housekeeping
from rcbot.application.housekeeping import (
    COMPACT_EVERY,
    DB_RETENTION,
    EVERY,
    FILE_RETENTION,
    LOG_RETENTION,
    Housekeeping,
)
from rcbot.config.settings import Settings
from rcbot.infrastructure import gubuy
from rcbot.infrastructure.gubuy import dom_source, quarantine
from rcbot.infrastructure.logging import setup as logging_setup
from tests.fakes import T0, FakeClock, MemoryKV

SPEC = (Path(__file__).resolve().parents[2] / "SPEC.md").read_text(encoding="utf-8")


def test_сроки_бд_как_в_таблице_spec() -> None:
    assert {
        "incidents": timedelta(days=180),
        "poll_stats_hourly": timedelta(days=30),
        "app_events": timedelta(days=30),
    } == DB_RETENTION
    for phrase in (
        "| Инциденты и доставки | БД | 180 дней |",
        "| Статистика опроса |",
        "30 дней |",
    ):
        assert phrase in SPEC


def test_сроки_файлов_как_в_таблице_spec() -> None:
    assert {
        "discovery": (timedelta(days=7), None),
        "quarantine": (timedelta(days=7), 20),
        "shots": (timedelta(days=3), 20),
    } == FILE_RETENTION
    assert "`data/quarantine/` | 7 дней, не больше 20 файлов" in SPEC
    assert "`data/shots/` | 3 дня, не больше 20 файлов" in SPEC
    assert "`data/discovery/` | 7 дней" in SPEC
    assert quarantine.MAX_FILES == 20
    assert dom_source.MAX_SHOTS == 20
    assert (
        quarantine.MAX_FILES is dom_source.MAX_SHOTS or quarantine.MAX_FILES == dom_source.MAX_SHOTS
    )
    assert gubuy.MAX_STORED_FILES == 20


def test_логи_ротация_пять_по_два_мегабайта_и_четырнадцать_дней() -> None:
    assert logging_setup.MAX_BYTES == 2 * 1024 * 1024
    assert logging_setup.BACKUPS == 5
    assert timedelta(days=14) == LOG_RETENTION
    assert "Ротация 5 файлов по 2 МБ; файлы старше 14 дней удаляются" in SPEC


def test_обработчик_логов_настроен_на_пять_файлов_по_два_мегабайта(tmp_path: Path) -> None:
    logging_setup.setup_logging(tmp_path / "logs" / "rcbot.log", console=False)
    (handler,) = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert handler.maxBytes == 2 * 1024 * 1024
    assert handler.backupCount == 5


def test_очистка_каждые_шесть_часов_vacuum_раз_в_неделю() -> None:
    assert timedelta(hours=6) == EVERY
    assert timedelta(days=7) == COMPACT_EVERY
    assert "каждые 6 часов" in SPEC
    assert "раз в неделю делает `PRAGMA wal_checkpoint(TRUNCATE)` и `VACUUM`" in SPEC


def test_журнал_на_рабочем_столе_квота_по_решению_r6() -> None:
    settings = Settings()
    assert (settings.journal_max_gb, settings.journal_trim_to_gb) == (10.0, 5.0)
    assert housekeeping.JOURNAL_QUOTA_EVERY_SEC == 600.0


# --- поведение ---


class _Maintenance:
    def __init__(self) -> None:
        self.cutoffs: list[dict[str, object]] = []
        self.compactions = 0

    async def delete_older_than(self, cutoffs: dict[str, object]) -> dict[str, int]:
        self.cutoffs.append(cutoffs)
        return {}

    async def compact(self) -> None:
        self.compactions += 1


class _Files:
    def __init__(self) -> None:
        self.pruned: list[tuple[str, timedelta, int | None]] = []
        self.cleared: list[str] = []
        self.log_cutoffs: list[timedelta] = []

    def prune(
        self, name: str, max_age: timedelta, now: object, max_files: int | None = None
    ) -> int:
        self.pruned.append((name, max_age, max_files))
        return 0

    def clear(self, name: str) -> int:
        self.cleared.append(name)
        return 0

    def prune_logs(self, max_age: timedelta, now: object) -> int:
        self.log_cutoffs.append(max_age)
        return 0

    def folder_size(self) -> int:
        return 0


async def test_очистка_применяет_сроки_от_текущего_момента() -> None:
    clock, maintenance, files = FakeClock(), _Maintenance(), _Files()
    keeper = Housekeeping(maintenance, files, MemoryKV(), clock)  # type: ignore[arg-type]
    await keeper.run_once()
    assert maintenance.cutoffs == [{t: T0 - keep for t, keep in DB_RETENTION.items()}]
    assert {(n, a, m) for n, a, m in files.pruned} == {
        (n, a, m) for n, (a, m) in FILE_RETENTION.items()
    }
    assert files.log_cutoffs == [LOG_RETENTION]


async def test_временные_файлы_очищаются_при_каждом_запуске() -> None:
    files = _Files()
    Housekeeping(_Maintenance(), files, MemoryKV(), FakeClock()).start()  # type: ignore[arg-type]
    assert files.cleared == ["tmp"]


@pytest.mark.parametrize(("after", "expected"), [(0, 1), (7 * 86400 - 1, 1), (7 * 86400, 2)])
async def test_уплотнение_бд_не_чаще_раза_в_неделю(after: int, expected: int) -> None:
    clock, maintenance = FakeClock(), _Maintenance()
    keeper = Housekeeping(maintenance, _Files(), MemoryKV(), clock)  # type: ignore[arg-type]
    await keeper.run_once()
    clock.advance(after)
    await keeper.run_once()
    assert maintenance.compactions == expected


async def test_каждая_проверка_не_пишется_в_бд_только_почасовая_статистика() -> None:
    """SPEC 2.2, 2.7: в таблицах БД нет «проверки»; есть почасовая poll_stats_hourly."""
    from rcbot.infrastructure.db import tables

    names = set(tables.Base.metadata.tables)
    assert names == {
        "watch_targets",
        "incidents",
        "incident_slots",
        "deliveries",
        "poll_stats_hourly",
        "app_events",
        "kv_settings",
    }
    assert {c.name for c in tables.Base.metadata.tables["poll_stats_hourly"].columns} == {
        "hour",
        "checks",
        "errors",
        "relogins",
        "p50_ms",
        "p95_ms",
    }
