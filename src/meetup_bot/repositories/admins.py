"""Запросы к таблице админов (проверка прав — на каждом админском действии)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Admin, AdminRole
from meetup_bot.services.errors import PermissionDenied


async def get_role(session: AsyncSession, user_id: int) -> str | None:
    return await session.scalar(select(Admin.role).where(Admin.user_id == user_id))


async def is_admin(session: AsyncSession, user_id: int) -> bool:
    """Админ или владелец."""
    return await get_role(session, user_id) is not None


async def is_owner(session: AsyncSession, user_id: int) -> bool:
    return await get_role(session, user_id) == AdminRole.OWNER.value


async def list_admin_ids(session: AsyncSession) -> list[int]:
    return list((await session.scalars(select(Admin.user_id).order_by(Admin.added_at))).all())


async def require_admin(session: AsyncSession, user_id: int) -> None:
    """Бросает PermissionDenied, если не админ. Вызывать в каждой админской операции."""
    if not await is_admin(session, user_id):
        raise PermissionDenied(f"пользователь {user_id} не админ")


async def require_owner(session: AsyncSession, user_id: int) -> None:
    if not await is_owner(session, user_id):
        raise PermissionDenied(f"пользователь {user_id} не владелец")
