"""Общие помощники диалогов: доступ к сервисам и проверка прав."""

from __future__ import annotations

from typing import Any

from aiogram_dialog import DialogManager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.container import Services
from meetup_bot.telegram.common import check_admin


def get_services(manager: DialogManager) -> Services:
    services: Services = manager.middleware_data["services"]
    return services


def get_sessions(manager: DialogManager) -> async_sessionmaker[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = manager.middleware_data["sessions"]
    return sessions


def actor_id(manager: DialogManager) -> int:
    """ID пользователя, нажавшего кнопку (не чата)."""
    user = manager.middleware_data["event_from_user"]
    return int(user.id)


async def is_admin(manager: DialogManager) -> bool:
    """Права перепроверяются на каждом действии: скрытая кнопка защитой не считается."""
    return await check_admin(get_sessions(manager), actor_id(manager))


def middleware_value(manager: DialogManager, key: str) -> Any:
    return manager.middleware_data[key]
