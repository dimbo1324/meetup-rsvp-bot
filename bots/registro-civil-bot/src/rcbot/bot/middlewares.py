"""Бот отвечает только владельцу (SPEC 2.4–2.6): остальные обновления молча отбрасываются."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_chat_id: int) -> None:
        self._owner = owner_chat_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        chat_id: int | None = None
        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else None
            chat_id = event.chat.id
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id
            chat_id = event.message.chat.id if event.message else user_id
        if user_id == self._owner and chat_id == self._owner:
            return await handler(event, data)
        # Номер чужого пользователя в журнал не пишется: фильтр логов вырезал бы его всё равно
        logger.info("отброшено обновление не от владельца (%s)", type(event).__name__)
        return None
