"""Окружение Alembic: асинхронный SQLite, пакетные ALTER (render_as_batch)."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from rcbot.config.paths import Paths
from rcbot.infrastructure.db.engine import create_engine
from rcbot.infrastructure.db.tables import Base

config = context.config
# При вызове из бота логирование уже настроено: конфигурацию логов alembic.ini не трогаем
if config.config_file_name is not None and not config.attributes.get("skip_logging"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
DATABASE_URL = config.attributes.get("database_url") or os.environ.get(
    "RCBOT_DATABASE_URL", Paths.default().database_url()
)


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL, target_metadata=target_metadata, literal_binds=True, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(DATABASE_URL)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
