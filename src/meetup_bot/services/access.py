"""Проверка прав в слое сервисов: вызывается в каждой админской операции (раздел 3)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.repositories import admins
from meetup_bot.services.errors import PermissionDenied


async def require_admin(session: AsyncSession, user_id: int | None) -> None:
    """Бросает PermissionDenied, если пользователь не админ и не владелец."""
    if user_id is None or not await admins.is_admin(session, user_id):
        raise PermissionDenied(f"пользователь {user_id} не админ")


async def require_owner(session: AsyncSession, user_id: int | None) -> None:
    if user_id is None or not await admins.is_owner(session, user_id):
        raise PermissionDenied(f"пользователь {user_id} не владелец")
