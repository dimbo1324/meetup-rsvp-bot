"""Запросы к таблице банов."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Ban


async def is_banned(session: AsyncSession, user_id: int) -> bool:
    found = await session.scalar(
        select(Ban.id).where(Ban.user_id == user_id, Ban.active.is_(True)).limit(1)
    )
    return found is not None
