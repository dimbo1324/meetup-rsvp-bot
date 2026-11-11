"""Запросы к таблице встреч."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Event, EventStatus, Registration, RegistrationStatus


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


async def unevaluated_marked_ids(session: AsyncSession) -> list[int]:
    """Встречи с отмеченной явкой, по которым пересчёт банов ещё не завершён (кроме отменённых)."""
    rows = await session.scalars(
        select(Event.id)
        .where(
            Event.attendance_marked_at.is_not(None),
            Event.bans_evaluated_at.is_(None),
            Event.status != EventStatus.CANCELLED.value,
        )
        .order_by(Event.id)
    )
    return list(rows.all())


async def expired_unmarked_ids(session: AsyncSession, started_before: datetime) -> list[int]:
    """Завершённые встречи без отметки явки, начавшиеся раньше `started_before`."""
    rows = await session.scalars(
        select(Event.id)
        .where(
            Event.attendance_marked_at.is_(None),
            Event.status == EventStatus.FINISHED.value,
            Event.starts_at < started_before,
        )
        .order_by(Event.id)
    )
    return list(rows.all())
