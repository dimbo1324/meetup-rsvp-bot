"""Запросы к таблице банов и счётчик неявок для автобана (раздел 5.8)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Attendance, Ban, Event, EventStatus, Registration


async def is_banned(session: AsyncSession, user_id: int) -> bool:
    found = await session.scalar(
        select(Ban.id).where(Ban.user_id == user_id, Ban.active.is_(True)).limit(1)
    )
    return found is not None


async def get_active(session: AsyncSession, user_id: int) -> Ban | None:
    return await session.scalar(select(Ban).where(Ban.user_id == user_id, Ban.active.is_(True)))


async def list_active(session: AsyncSession) -> list[Ban]:
    rows = await session.scalars(
        select(Ban).where(Ban.active.is_(True)).order_by(Ban.banned_at, Ban.id)
    )
    return list(rows.all())


async def history(session: AsyncSession, user_id: int) -> list[Ban]:
    rows = await session.scalars(
        select(Ban).where(Ban.user_id == user_id).order_by(Ban.banned_at, Ban.id)
    )
    return list(rows.all())


async def last_unban_at(session: AsyncSession, user_id: int) -> datetime | None:
    return await session.scalar(select(func.max(Ban.unbanned_at)).where(Ban.user_id == user_id))


async def count_no_shows(session: AsyncSession, user_id: int) -> int:
    """Неявки, учитываемые правилом бана.

    Не считаются поздние продвижения из очереди (человек не мог планировать), отменённые
    встречи и неявки, отмеченные до последнего разбана (после разбана счётчик с нуля).
    """
    query = (
        select(func.count())
        .select_from(Registration)
        .join(Event, Registration.event_id == Event.id)
        .where(
            Registration.user_id == user_id,
            Registration.attendance == Attendance.NO_SHOW.value,
            Registration.late_promoted.is_(False),
            Event.status != EventStatus.CANCELLED.value,
        )
    )
    unbanned = await last_unban_at(session, user_id)
    if unbanned is not None:
        query = query.where(Event.attendance_marked_at > unbanned)
    else:
        query = query.where(Event.attendance_marked_at.is_not(None))
    return int(await session.scalar(query) or 0)
