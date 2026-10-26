"""Запросы к таблице записей."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import (
    LIVE_REGISTRATION_STATUSES,
    UNFINISHED_EVENT_STATUSES,
    Attendance,
    Event,
    Registration,
    RegistrationStatus,
    User,
)


async def get_live(session: AsyncSession, event_id: int, user_id: int) -> Registration | None:
    """Активная запись или место в листе ожидания пользователя на встрече."""
    return await session.scalar(
        select(Registration).where(
            Registration.event_id == event_id,
            Registration.user_id == user_id,
            Registration.status.in_(LIVE_REGISTRATION_STATUSES),
        )
    )


async def waitlist_position(session: AsyncSession, registration: Registration) -> int:
    """Номер в очереди (с 1): порядок по времени создания, затем по id."""
    ahead = await session.scalar(
        select(func.count()).where(
            Registration.event_id == registration.event_id,
            Registration.status == RegistrationStatus.WAITLIST.value,
            or_(
                Registration.created_at < registration.created_at,
                (Registration.created_at == registration.created_at)
                & (Registration.id <= registration.id),
            ),
        )
    )
    return int(ahead or 0)


async def list_waitlist(session: AsyncSession, event_id: int) -> list[Registration]:
    """Очередь встречи в порядке постановки (created_at, затем id)."""
    rows = await session.scalars(
        select(Registration)
        .where(
            Registration.event_id == event_id,
            Registration.status == RegistrationStatus.WAITLIST.value,
        )
        .order_by(Registration.created_at, Registration.id)
    )
    return list(rows.all())


async def count_waitlist(session: AsyncSession, event_id: int) -> int:
    """Сколько человек стоит в очереди встречи."""
    count = await session.scalar(
        select(func.count()).where(
            Registration.event_id == event_id,
            Registration.status == RegistrationStatus.WAITLIST.value,
        )
    )
    return int(count or 0)


async def list_active_with_users(
    session: AsyncSession, event_id: int
) -> list[tuple[Registration, User | None]]:
    """Активные записи встречи в порядке записи вместе с данными пользователей."""
    rows = await session.execute(
        select(Registration, User)
        .outerjoin(User, Registration.user_id == User.id)
        .where(
            Registration.event_id == event_id,
            Registration.status == RegistrationStatus.ACTIVE.value,
        )
        .order_by(Registration.created_at, Registration.id)
    )
    return [(registration, user) for registration, user in rows.all()]


async def no_show_user_ids(session: AsyncSession, event_id: int) -> list[int]:
    """Пользователи с неявкой на встрече: активные и отменённые админом «как неявка»."""
    rows = await session.scalars(
        select(Registration.user_id)
        .where(
            Registration.event_id == event_id,
            Registration.attendance == Attendance.NO_SHOW.value,
            Registration.status.in_(
                (RegistrationStatus.ACTIVE.value, RegistrationStatus.CANCELLED.value)
            ),
            Registration.user_id.is_not(None),
        )
        .order_by(Registration.id)
    )
    return list(dict.fromkeys(user_id for user_id in rows.all() if user_id is not None))


async def get(session: AsyncSession, registration_id: int) -> Registration | None:
    return await session.get(Registration, registration_id)


async def list_active(session: AsyncSession, event_id: int) -> list[Registration]:
    """Активные записи встречи (без очереди) для массовой отметки явки."""
    rows = await session.scalars(
        select(Registration).where(
            Registration.event_id == event_id,
            Registration.status == RegistrationStatus.ACTIVE.value,
        )
    )
    return list(rows.all())


async def list_live_of_user(
    session: AsyncSession, event_id: int, user_id: int
) -> list[Registration]:
    """Все живые записи пользователя на встрече (активная и/или очередь)."""
    rows = await session.scalars(
        select(Registration).where(
            Registration.event_id == event_id,
            Registration.user_id == user_id,
            Registration.status.in_(LIVE_REGISTRATION_STATUSES),
        )
    )
    return list(rows.all())


async def upcoming_of_user(session: AsyncSession, user_id: int) -> list[tuple[Registration, Event]]:
    """Живые записи пользователя на встречи, которые ещё не завершились, по времени начала."""
    rows = await session.execute(
        select(Registration, Event)
        .join(Event, Registration.event_id == Event.id)
        .where(
            Registration.user_id == user_id,
            Registration.status.in_(LIVE_REGISTRATION_STATUSES),
            Event.status.in_(UNFINISHED_EVENT_STATUSES),
        )
        .order_by(Event.starts_at, Registration.id)
    )
    return [(registration, event) for registration, event in rows.all()]


async def future_event_ids_of_user(session: AsyncSession, user_id: int, now: datetime) -> set[int]:
    """Незавершённые встречи, которые ещё не начались и где у пользователя живая запись."""
    rows = await session.scalars(
        select(Registration.event_id)
        .join(Event, Registration.event_id == Event.id)
        .where(
            Registration.user_id == user_id,
            Registration.status.in_(LIVE_REGISTRATION_STATUSES),
            Event.status.in_(UNFINISHED_EVENT_STATUSES),
            Event.starts_at > now,
        )
    )
    return set(rows.all())
