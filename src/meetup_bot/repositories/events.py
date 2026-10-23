"""Запросы к таблице встреч."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Event, Registration, RegistrationStatus


async def get(session: AsyncSession, event_id: int) -> Event | None:
    return await session.get(Event, event_id)


async def seats_taken(session: AsyncSession, event_id: int) -> int:
    """Занято мест: сумма `1 + guests` по активным записям."""
    total = await session.scalar(
        select(func.coalesce(func.sum(1 + Registration.guests), 0)).where(
            Registration.event_id == event_id,
            Registration.status == RegistrationStatus.ACTIVE.value,
        )
    )
    return int(total or 0)
