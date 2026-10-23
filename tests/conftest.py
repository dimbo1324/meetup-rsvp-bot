from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meetup_bot.clock import FakeClock
from meetup_bot.db import models  # noqa: F401  (регистрирует таблицы в метаданных)
from meetup_bot.db.base import Base
from meetup_bot.db.session import create_engine, create_session_factory
from meetup_bot.services.events import EventService


@pytest.fixture
def clock() -> FakeClock:
    # Пятница 12:00 UTC (09:00 в Монтевидео): до дедлайна отмены ближайшей субботы
    return FakeClock(datetime(2026, 10, 23, 12, 0, tzinfo=UTC))


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Настоящая файловая SQLite: проверяет блокировки и транзакции, как в бою."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(db_engine)


@pytest.fixture
def event_service(sessions: async_sessionmaker[AsyncSession], clock: FakeClock) -> EventService:
    return EventService(sessions, clock)
