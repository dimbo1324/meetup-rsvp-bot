"""Запросы к таблице встреч."""

from __future__ import annotations

from collections.abc import Sequence

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


async def seats_taken_map(session: AsyncSession, event_ids: Sequence[int]) -> dict[int, int]:
    """Занятые места сразу по нескольким встречам одним запросом (без N+1)."""
    if not event_ids:
        return {}
    rows = await session.execute(
        select(Registration.event_id, func.sum(1 + Registration.guests))
        .where(
            Registration.event_id.in_(event_ids),
            Registration.status == RegistrationStatus.ACTIVE.value,
        )
        .group_by(Registration.event_id)
    )
    taken = {event_id: int(total or 0) for event_id, total in rows.all()}
    return {event_id: taken.get(event_id, 0) for event_id in event_ids}
