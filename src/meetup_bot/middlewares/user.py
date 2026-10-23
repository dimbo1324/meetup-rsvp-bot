"""Актуализация данных пользователя при каждом взаимодействии."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.repositories import users
from meetup_bot.repositories.users import UserInfo

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    """Обновляет имя и @username в БД и кладёт `user_info` в данные обработчика.

    Имена в постах берутся из БД, поэтому без этого смена имени в Telegram не доходила бы
    до списка участников. Сбой записи не должен блокировать обработку: данные уже есть.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        # Боты (в том числе анонимные админы групп) в записях не участвуют
        if tg_user is None or tg_user.is_bot:
            return None
        info = UserInfo(
            id=tg_user.id,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            username=tg_user.username,
        )
        sessions: async_sessionmaker[AsyncSession] = data["sessions"]
        clock: Clock = data["clock"]
        try:
            async with sessions() as session, session.begin():
                await users.upsert(session, info, clock.now())
        except Exception:
            logger.exception("не удалось обновить данные пользователя %s", info.id)
        data["user_info"] = info
        return await handler(event, data)
