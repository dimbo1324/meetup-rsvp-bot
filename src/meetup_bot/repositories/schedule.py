"""Выборки «что пора сделать по времени» для планировщика и жизненного цикла встреч."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import UNFINISHED_EVENT_STATUSES, Event, EventStatus


async def ids_to_close(session: AsyncSession, now: datetime) -> list[int]:
    """Открытые встречи, у которых наступило закрытие записи."""
    rows = await session.scalars(
        select(Event.id)
        .where(Event.status == EventStatus.OPEN.value, Event.registration_closes_at <= now)
        .order_by(Event.id)
    )
    return list(rows.all())


async def ids_to_finish(session: AsyncSession, now: datetime) -> list[int]:
    """Незавершённые встречи, у которых наступили и начало, и закрытие записи."""
    rows = await session.scalars(
        select(Event.id)
        .where(
            Event.status.in_(UNFINISHED_EVENT_STATUSES),
            Event.starts_at <= now,
            Event.registration_closes_at <= now,
        )
        .order_by(Event.id)
    )
    return list(rows.all())


async def recurrence_key_exists(session: AsyncSession, key: str) -> bool:
    found = await session.scalar(select(Event.id).where(Event.recurrence_key == key))
    return found is not None


async def due_reminders(session: AsyncSession, now: datetime) -> list[Event]:
    """Встречи, которым пора напомнить участникам и которым ещё не напоминали."""
    rows = await session.scalars(
        select(Event)
        .where(
            Event.status.in_(UNFINISHED_EVENT_STATUSES),
            Event.reminder_at <= now,
            Event.reminder_sent_at.is_(None),
        )
        .order_by(Event.id)
    )
    return list(rows.all())


async def due_attendance_prompts(session: AsyncSession, now: datetime) -> list[Event]:
    """Завершённые встречи без отметки явки, которым пора отправить запрос явки."""
    rows = await session.scalars(
        select(Event)
        .where(
            Event.status == EventStatus.FINISHED.value,
            Event.attendance_marked_at.is_(None),
            Event.attendance_prompt_at <= now,
            Event.attendance_prompt_sent_at.is_(None),
        )
        .order_by(Event.id)
    )
    return list(rows.all())


async def due_attendance_reminders(
    session: AsyncSession, now: datetime, *, after: timedelta
) -> list[Event]:
    """Встречи, где явку так и не отметили спустя `after` после запроса."""
    rows = await session.scalars(
        select(Event)
        .where(
            Event.status == EventStatus.FINISHED.value,
            Event.attendance_prompt_sent_at.is_not(None),
            Event.attendance_prompt_sent_at <= now - after,
            Event.attendance_marked_at.is_(None),
            Event.attendance_reminder_sent_at.is_(None),
        )
        .order_by(Event.id)
    )
    return list(rows.all())
