"""Движок и фабрика сессий; настройки SQLite выставляются при каждом подключении."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    # Автокоммит на уровне драйвера: транзакциями управляем сами (см. _begin_immediate)
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _begin_immediate(connection: Any) -> None:
    # Блокировка записи берётся сразу, а не при первой записи: проверка мест и вставка
    # записи не могут выполниться в двух транзакциях одновременно (раздел 9.3)
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
        event.listen(engine.sync_engine, "begin", _begin_immediate)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
