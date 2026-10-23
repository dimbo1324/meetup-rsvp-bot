"""Окружение Alembic: асинхронный SQLite, пакетные операции ALTER (render_as_batch)."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from meetup_bot.db.base import Base
from meetup_bot.db.session import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/bot.db")


def _ensure_sqlite_dir() -> None:
    prefix = "sqlite+aiosqlite:///"
    if DATABASE_URL.startswith(prefix) and ":memory:" not in DATABASE_URL:
        path = DATABASE_URL.removeprefix(prefix)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    _ensure_sqlite_dir()
    engine = create_engine(DATABASE_URL)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
