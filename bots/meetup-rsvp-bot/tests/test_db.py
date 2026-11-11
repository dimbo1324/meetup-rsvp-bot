from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, Integer, Table, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from meetup_bot.db.base import Base
from meetup_bot.db.types import UtcDateTime

sample = Table(
    "sample",
    Base.metadata,
    Column("id", Integer, primary_key=True),
    Column("at", UtcDateTime, nullable=False),
)


async def test_pragma_выставлены(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 5000


async def test_utc_datetime_круговой_обмен(engine: AsyncEngine) -> None:
    moment = datetime(2026, 10, 24, 17, 0, tzinfo=timezone(timedelta(hours=-3)))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(sample.insert().values(id=1, at=moment))
        stored = (await conn.execute(select(sample.c.at))).scalar_one()
    assert stored == datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
    assert stored.utcoffset() == timedelta(0)


async def test_utc_datetime_отвергает_naive(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        with pytest.raises(Exception, match="naive"):
            await conn.execute(sample.insert().values(id=2, at=datetime(2026, 1, 1)))
