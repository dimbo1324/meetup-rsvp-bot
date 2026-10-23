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
from tests.helpers import App, make_app
from tests.tg_fake import Harness


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


@pytest.fixture
async def app(tmp_path: Path, clock: FakeClock) -> AsyncIterator[App]:
    """Полное приложение на фейковых часах (пятница 12:00 UTC); владелец id=1, админ id=2."""
    application = await make_app(tmp_path, clock)
    yield application
    await application.engine.dispose()


@pytest.fixture
async def tg(app: App) -> AsyncIterator[Harness]:
    """Диспетчер с хендлерами и диалогами поверх фейковой сессии бота."""
    harness = Harness(app)
    yield harness
    await harness.close()
