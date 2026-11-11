"""Запросы к таблице админов (проверка прав — на каждом админском действии)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Admin, AdminRole, User


async def get_role(session: AsyncSession, user_id: int) -> str | None:
    return await session.scalar(select(Admin.role).where(Admin.user_id == user_id))


async def is_admin(session: AsyncSession, user_id: int) -> bool:
    """Админ или владелец."""
    return await get_role(session, user_id) is not None


async def is_owner(session: AsyncSession, user_id: int) -> bool:
    return await get_role(session, user_id) == AdminRole.OWNER.value


async def list_admin_ids(session: AsyncSession) -> list[int]:
    return list((await session.scalars(select(Admin.user_id).order_by(Admin.added_at))).all())


async def get(session: AsyncSession, user_id: int) -> Admin | None:
    return await session.get(Admin, user_id)


async def list_with_users(session: AsyncSession) -> list[tuple[Admin, User]]:
    rows = await session.execute(
        select(Admin, User)
        .join(User, Admin.user_id == User.id)
        .order_by(Admin.added_at, Admin.user_id)
    )
    return [(admin, user) for admin, user in rows.all()]


async def insert(
    session: AsyncSession, *, user_id: int, role: AdminRole, added_by: int | None, now: datetime
) -> Admin:
    admin = Admin(user_id=user_id, role=role.value, added_by=added_by, added_at=now)
    session.add(admin)
    await session.flush()
    return admin


async def delete(session: AsyncSession, admin: Admin) -> None:
    await session.delete(admin)
    await session.flush()
