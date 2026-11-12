"""Миграции Alembic: схема SPEC 3.2 строится на чистой базе и не расходится с моделями."""

from __future__ import annotations

import asyncio
import configparser
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from rcbot.bootstrap import upgrade_database, upgrade_database_async
from rcbot.config.paths import Paths
from tests.conftest import BOT_DIR

TABLES = {
    "watch_targets",
    "incidents",
    "incident_slots",
    "deliveries",
    "poll_stats_hourly",
    "app_events",
    "kv_settings",
}
INDEXES = {"ix_app_events_ts", "ix_incidents_target_status", "ix_deliveries_incident"}


def config_for(paths: Paths) -> Config:
    config = Config(str(paths.alembic_ini))
    config.attributes["database_url"] = paths.database_url()
    config.attributes["skip_logging"] = True
    return config


def names(paths: Paths, kind: str) -> set[str]:
    with sqlite3.connect(paths.database) as conn:
        rows = conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='{kind}' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row[0] for row in rows}


@pytest.fixture
def migrated(paths: Paths) -> Paths:
    paths.ensure()
    upgrade_database(paths)
    return paths


def test_чистая_база_получает_все_таблицы_и_индексы_из_spec(migrated: Paths) -> None:
    assert names(migrated, "table") - {"alembic_version"} == TABLES
    assert names(migrated, "index") >= INDEXES


def test_версия_схемы_записана(migrated: Paths) -> None:
    with sqlite3.connect(migrated.database) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [("0001",)]


def test_alembic_check_не_видит_расхождений_между_моделями_и_миграциями(migrated: Paths) -> None:
    # Падает с CommandError, если модели в tables.py изменили, а миграцию не добавили
    command.check(config_for(migrated))


def test_повторный_upgrade_идемпотентен_и_не_трогает_данные(migrated: Paths) -> None:
    with sqlite3.connect(migrated.database) as conn:
        conn.execute("INSERT INTO kv_settings (key, value) VALUES ('interval', '20')")
    upgrade_database(migrated)
    upgrade_database(migrated)
    with sqlite3.connect(migrated.database) as conn:
        assert conn.execute("SELECT key, value FROM kv_settings").fetchall() == [("interval", "20")]
    assert names(migrated, "table") - {"alembic_version"} == TABLES


def test_downgrade_до_base_убирает_всё_а_повторный_upgrade_возвращает(migrated: Paths) -> None:
    config = config_for(migrated)
    command.downgrade(config, "base")
    assert names(migrated, "table") == {"alembic_version"}
    assert names(migrated, "index") == set()
    command.upgrade(config, "head")
    assert names(migrated, "table") - {"alembic_version"} == TABLES
    assert names(migrated, "index") >= INDEXES
    command.check(config)


def test_внешние_ключи_каскады_заданы_в_схеме(migrated: Paths) -> None:
    with sqlite3.connect(migrated.database) as conn:
        for table in ("incident_slots", "deliveries"):
            (fk,) = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert (fk[2], fk[3], fk[4], fk[6]) == (
                "incidents",
                "incident_id",
                "id",
                "CASCADE",
            ), table


def test_в_схеме_нет_колонок_под_секреты_и_личные_данные(migrated: Paths) -> None:
    """SPEC 3.2: секретов и личных данных в БД нет — имена колонок не должны их обещать."""
    forbidden = ("password", "token", "cookie", "secret", "email", "phone", "document", "passport")
    with sqlite3.connect(migrated.database) as conn:
        for table in TABLES:
            for _, column, *_ in conn.execute(f"PRAGMA table_info({table})"):
                assert not any(word in column.lower() for word in forbidden), (table, column)


def test_время_в_схеме_хранится_строкой_а_не_числом(migrated: Paths) -> None:
    with sqlite3.connect(migrated.database) as conn:
        columns = {
            (table, name): kind
            for table in ("incidents", "deliveries", "app_events", "poll_stats_hourly")
            for _, name, kind, *_ in conn.execute(f"PRAGMA table_info({table})")
        }
    for key in (
        ("incidents", "opened_at"),
        ("incidents", "closed_at"),
        ("incidents", "acked_at"),
        ("deliveries", "at"),
        ("app_events", "ts"),
        ("poll_stats_hourly", "hour"),
    ):
        assert columns[key].upper().startswith("VARCHAR"), key


def test_alembic_ini_бота_только_ascii_и_читается_как_cp1252() -> None:
    # configparser на Windows читает файл в кодировке по умолчанию (cp1252): кириллица ломает запуск
    raw = (BOT_DIR / "alembic.ini").read_bytes()
    raw.decode("ascii")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(raw.decode("cp1252"))
    assert parser.has_section("alembic")


def test_настоящий_alembic_ini_бота_находит_скрипты_миграций(tmp_path: Path) -> None:
    config = Config(str(BOT_DIR / "alembic.ini"))
    db_file = tmp_path / "real.sqlite3"
    config.attributes["database_url"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    config.attributes["skip_logging"] = True
    command.upgrade(config, "head")
    with sqlite3.connect(db_file) as conn:
        found = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert found >= TABLES


async def test_upgrade_из_работающего_цикла_не_блокирует_его(paths: Paths) -> None:
    paths.ensure()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await upgrade_database_async(paths)
    finally:
        task.cancel()
    assert ticks > 5  # цикл жил, пока миграции шли в потоке
    assert names(paths, "table") - {"alembic_version"} == TABLES


async def test_два_upgrade_подряд_из_цикла_безопасны(paths: Paths) -> None:
    paths.ensure()
    await upgrade_database_async(paths)
    await upgrade_database_async(paths)
    assert names(paths, "table") - {"alembic_version"} == TABLES


def test_база_создаётся_в_wal_после_первого_подключения_бота(migrated: Paths) -> None:
    """Режим WAL хранится в самом файле: после миграций с create_engine он уже включён."""
    with sqlite3.connect(migrated.database) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
