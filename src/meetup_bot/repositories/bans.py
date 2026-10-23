"""Запросы к таблице банов и счётчик неявок для автобана (раздел 5.8)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import (
    Attendance,
    Ban,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
)


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
    """Время последнего РУЧНОГО разбана.

    Автоснятие бана при исправлении явки (`auto_removed`) счётчик не обнуляет: иначе
    исправление одной отметки стирало бы остальные неявки пользователя.
    """
    return await session.scalar(
        select(func.max(Ban.unbanned_at)).where(Ban.user_id == user_id, Ban.auto_removed.is_(False))
    )


def no_show_moment() -> ColumnElement[datetime]:
    """Момент неявки записи: отмена админом «как неявка» раньше отметки явки встречи."""
    return func.coalesce(Registration.cancelled_at, Event.attendance_marked_at)


async def count_no_shows(session: AsyncSession, user_id: int) -> int:
    """Неявки, учитываемые правилом бана.

    Не считаются поздние продвижения из очереди (человек не мог планировать), отменённые
    встречи и неявки до последнего ручного разбана (после него счётчик с нуля). Неявкой
    считается и запись status=cancelled с attendance=no_show (отмена админом после дедлайна).
    """
    moment = no_show_moment()
    query = (
        select(func.count())
        .select_from(Registration)
        .join(Event, Registration.event_id == Event.id)
        .where(
            Registration.user_id == user_id,
            Registration.attendance == Attendance.NO_SHOW.value,
            Registration.status.in_(
                (RegistrationStatus.ACTIVE.value, RegistrationStatus.CANCELLED.value)
            ),
            Registration.late_promoted.is_(False),
            Event.status != EventStatus.CANCELLED.value,
            moment.is_not(None),
        )
    )
    unbanned = await last_unban_at(session, user_id)
    if unbanned is not None:
        query = query.where(moment > unbanned)
    return int(await session.scalar(query) or 0)
