"""Проверка служебного чата при запуске: в него уходят бэкапы с персональными данными."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError

from meetup_bot.services.ports import Notifier
from meetup_bot.texts.service import SERVICE_CHAT_PUBLIC_TEXT, SERVICE_CHAT_UNKNOWN_TEXT

logger = logging.getLogger(__name__)

PRIVATE_CHAT_TYPES = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})


async def check_service_chat(bot: Bot, notifier: Notifier, chat_id: int) -> bool:
    """True, если чат закрытая группа; иначе предупреждает в лог и в сам чат."""
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramAPIError as exc:
        logger.error("служебный чат недоступен боту: %s", exc.message)
        await notifier.send_service(SERVICE_CHAT_UNKNOWN_TEXT)
        return False
    if chat.username or chat.type not in PRIVATE_CHAT_TYPES:
        logger.warning(
            "служебный чат публичный или не группа (type=%s): бэкапы с персональными данными "
            "могут увидеть посторонние",
            chat.type,
        )
        await notifier.send_service(SERVICE_CHAT_PUBLIC_TEXT)
        return False
    return True
