"""Реализация порта `Notifier` поверх Telegram Bot API."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.repositories import users

logger = logging.getLogger(__name__)

CHUNK_LIMIT = 4000


def split_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Режет по переносам строк, где возможно; слишком длинную строку — жёстко."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        service_chat_id: int,
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._clock = clock
        self._service_chat_id = service_chat_id

    async def send_dm(self, user_id: int, text: str) -> bool:
        for attempt in range(2):
            try:
                await self._bot.send_message(user_id, text)
            except TelegramForbiddenError:
                # Бот заблокирован или диалога нет: запоминаем, чтобы не стучаться снова
                async with self._sessions() as session, session.begin():
                    await users.set_dm_available(session, user_id, False, self._clock.now())
                return False
            except TelegramRetryAfter as exc:
                if attempt == 1:
                    logger.warning("лимит Telegram при отправке личного сообщения (%s)", user_id)
                    return False
                await asyncio.sleep(exc.retry_after)
            except TelegramAPIError as exc:
                logger.warning("личное сообщение не доставлено (%s): %s", user_id, exc.message)
                return False
            else:
                return True
        return False

    async def send_service(self, text: str) -> None:
        for chunk in split_text(text):
            try:
                await self._bot.send_message(self._service_chat_id, chunk)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                try:
                    await self._bot.send_message(self._service_chat_id, chunk)
                except TelegramAPIError as retry_exc:
                    logger.warning("служебное сообщение не доставлено: %s", retry_exc.message)
            except TelegramAPIError as exc:
                logger.warning("служебное сообщение не доставлено: %s", exc.message)

    async def send_attendance_prompt(self, admin_id: int, event_id: int) -> bool:
        # Меню отметки явки — отдельный этап
        logger.info("интерфейс отметки явки — веха M5 (admin=%s, event=%s)", admin_id, event_id)
        return False
