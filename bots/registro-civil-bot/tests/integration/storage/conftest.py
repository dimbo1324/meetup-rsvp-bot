"""Помощники интеграционных тестов хранения: настоящая файловая SQLite с применёнными миграциями."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rcbot.bootstrap import upgrade_database_async
from rcbot.config.paths import Paths
from rcbot.domain.models import WatchTarget
from rcbot.infrastructure.db.engine import create_engine, create_session_factory
from rcbot.infrastructure.db.repositories import (
    SqlEventLog,
    SqlIncidentRepository,
    SqlKeyValueStore,
    SqlMaintenance,
    SqlStatsRepository,
    SqlTargetRepository,
)


@dataclass
class Db:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    targets: SqlTargetRepository
    incidents: SqlIncidentRepository
    stats: SqlStatsRepository
    events: SqlEventLog
    kv: SqlKeyValueStore
    maintenance: SqlMaintenance
    target: WatchTarget


@pytest.fixture
async def db(paths: Paths) -> AsyncIterator[Db]:
    paths.ensure()
    await upgrade_database_async(paths)
    engine = create_engine(paths.database_url())
    sessions = create_session_factory(engine)
    targets = SqlTargetRepository(sessions)
    target = await targets.ensure("registro-civil", "https://example.test/agenda", "Oficina", 6)
    yield Db(
        engine=engine,
        sessions=sessions,
        targets=targets,
        incidents=SqlIncidentRepository(sessions),
        stats=SqlStatsRepository(sessions),
        events=SqlEventLog(sessions),
        kv=SqlKeyValueStore(sessions),
        maintenance=SqlMaintenance(engine, sessions),
        target=target,
    )
    await engine.dispose()
