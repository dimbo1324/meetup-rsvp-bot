from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from meetup_bot.clock import FakeClock
from meetup_bot.db.session import create_engine


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 23, 12, 0, tzinfo=UTC))


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    yield engine
    await engine.dispose()
