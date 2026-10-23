"""Запросы к таблице записей."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Registration, RegistrationStatus, User

LIVE_STATUSES = (RegistrationStatus.ACTIVE.value, RegistrationStatus.WAITLIST.value)


async def get_live(session: AsyncSession, event_id: int, user_id: int) -> Registration | None:
    """Активная запись или место в листе ожидания пользователя на встрече."""
    return await session.scalar(
        select(Registration).where(
            Registration.event_id == event_id,
            Registration.user_id == user_id,
            Registration.status.in_(LIVE_STATUSES),
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
