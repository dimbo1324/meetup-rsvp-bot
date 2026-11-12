"""Движок SQLite: WAL, synchronous=NORMAL, внешние ключи, ожидание блокировки (SPEC 2.7)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from rcbot.config.paths import Paths
from rcbot.domain.models import AppEvent, Channel, DeliveryResult
from tests.fakes import T0
from tests.integration.storage.conftest import Db


async def pragma(db: Db, name: str) -> object:
    async with db.engine.connect() as conn:
        return (await conn.execute(text(f"PRAGMA {name}"))).scalar()


async def test_режим_журнала_wal(db: Db) -> None:
    assert await pragma(db, "journal_mode") == "wal"


async def test_synchronous_normal(db: Db) -> None:
    assert await pragma(db, "synchronous") == 1  # 1 = NORMAL


async def test_внешние_ключи_включены_на_каждом_соединении(db: Db) -> None:
    # Два соединения одновременно — значит, второе новое, а не то же самое из пула
    async with db.engine.connect() as first, db.engine.connect() as second:
        for conn in (first, second):
            assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1


async def test_ожидание_блокировки_пять_секунд(db: Db) -> None:
    assert await pragma(db, "busy_timeout") == 5000


async def test_доставка_несуществующего_инцидента_отвергается_внешним_ключом(db: Db) -> None:
    result = DeliveryResult(Channel.TELEGRAM, True, 1.0, 1, "", incident_id=999)
    with pytest.raises(IntegrityError):
        await db.incidents.record_delivery(result, T0)


async def test_после_записи_рядом_с_базой_появляется_wal_файл(db: Db, paths: Paths) -> None:
    await db.kv.set("k", "v")
    assert paths.database.with_name(paths.database.name + "-wal").exists()


async def test_голый_движок_wal_не_включает_значит_режим_задан_кодом_бота(paths: Paths) -> None:
    paths.ensure()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(paths.data / 'bare.sqlite3').as_posix()}")
    async with engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
    await engine.dispose()
    assert mode == "delete"


async def test_двадцать_параллельных_записей_без_database_is_locked(db: Db) -> None:
    async def write(n: int) -> None:
        event = AppEvent(
            datetime(2026, 11, 12, 15, 0, n, tzinfo=UTC), "INFO", "t", f"сообщение {n}"
        )
        await db.events.write(event)

    await asyncio.gather(*(write(n) for n in range(20)))
    assert len(await db.events.last(100, "DEBUG")) == 20


async def test_параллельные_записи_разных_таблиц_не_блокируют_друг_друга(db: Db) -> None:
    async def event(n: int) -> None:
        await db.events.write(AppEvent(T0, "INFO", "t", str(n)))

    async def kv(n: int) -> None:
        await db.kv.set(f"k{n}", str(n))

    async def incident(n: int) -> None:
        await db.incidents.open(db.target.id, T0, frozenset())

    await asyncio.gather(*(f(n) for n in range(10) for f in (event, kv, incident)))
    assert len(await db.kv.all()) == 10
    assert len(await db.incidents.last(100)) == 10
