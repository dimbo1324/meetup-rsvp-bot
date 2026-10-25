"""Запросы для админских операций над встречами (списки, получатели уведомлений)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import (
    UNFINISHED_EVENT_STATUSES,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
    User,
)

ACTIVE_ONLY = (RegistrationStatus.ACTIVE.value,)


async def list_events(session: AsyncSession, *, recent_since: datetime | None) -> list[Event]:
    """Открытые и закрытые встречи; с `recent_since` — ещё и любые начавшиеся не раньше него."""
    live = Event.status.in_(UNFINISHED_EVENT_STATUSES)
    condition = live if recent_since is None else or_(live, Event.starts_at >= recent_since)
    found = await session.scalars(select(Event).where(condition).order_by(Event.starts_at.desc()))
    return list(found.all())


async def latest_not_cancelled(session: AsyncSession) -> Event | None:
    return await session.scalar(
        select(Event)
        .where(Event.status != EventStatus.CANCELLED.value)
        .order_by(Event.starts_at.desc(), Event.id.desc())
        .limit(1)
    )


async def get_registration(session: AsyncSession, registration_id: int) -> Registration | None:
    return await session.get(Registration, registration_id)


async def list_with_users(
    session: AsyncSession, event_id: int, statuses: tuple[str, ...] | None = None
) -> list[tuple[Registration, User | None]]:
    """Записи встречи (по умолчанию все статусы) в порядке записи вместе с пользователями."""
    query = (
        select(Registration, User)
        .outerjoin(User, Registration.user_id == User.id)
        .where(Registration.event_id == event_id)
        .order_by(Registration.created_at, Registration.id)
    )
    if statuses is not None:
        query = query.where(Registration.status.in_(statuses))
    return [(registration, user) for registration, user in (await session.execute(query)).all()]


async def dm_recipients(
    session: AsyncSession, event_id: int, statuses: tuple[str, ...]
) -> list[int]:
    """Пользователи записей встречи в нужных статусах, которым бот может писать."""
    rows = await session.scalars(
        select(User.id)
        .join(Registration, Registration.user_id == User.id)
        .where(
            Registration.event_id == event_id,
            Registration.status.in_(statuses),
            User.dm_available.is_(True),
        )
        .order_by(Registration.id)
    )
    return list(dict.fromkeys(rows.all()))
