"""Реализация порта `Notifier` поверх Telegram Bot API."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.repositories import users
from meetup_bot.telegram.attendance_ui import AttendanceUI, SheetStatus
from meetup_bot.telegram.common import MAX_RETRY_AFTER
from meetup_bot.texts.render import telegram_length
from meetup_bot.texts.service import (
    BACKUP_CAPTION,
    SERVICE_CHAT_MIGRATED_HINT,
    SERVICE_CHAT_UNAVAILABLE_TEXT,
)

logger = logging.getLogger(__name__)

CHUNK_LIMIT = 4000
# Предел Bot API для файлов, которые бот отправляет сам
UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024


# Неделимые куски HTML: тег и сущность; остальное — по одному символу
_ATOM_RE = re.compile(r"<[^>]*>|&#?\w+;|.", re.DOTALL)
_CHAT_NOT_FOUND = "chat not found"


def _split_long_line(line: str, limit: int) -> list[str]:
    """Режет одну длинную строку по границам тегов и сущностей, не разрывая их."""
    parts: list[str] = []
    current = ""
    size = 0
    for atom in _ATOM_RE.findall(line):
        atom_size = telegram_length(atom)
        # Тег длиннее лимита сохранить целым нельзя: режем по символам
        pieces = list(atom) if atom_size > limit else [atom]
        for piece in pieces:
            piece_size = telegram_length(piece)
            if size + piece_size > limit and current:
                parts.append(current)
                current, size = "", 0
            current += piece
            size += piece_size
    if current:
        parts.append(current)
    return parts


def split_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Режет по переносам строк; слишком длинную строку — вне тегов и сущностей HTML.

    Длина считается в единицах UTF-16, как её считает Telegram.
    """
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if telegram_length(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            *head, line = _split_long_line(line, limit)
            chunks.extend(head)
        candidate = f"{current}\n{line}" if current else line
        if telegram_length(candidate) > limit:
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
        owner_ids: Iterable[int] = (),
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._clock = clock
        self._service_chat_id = service_chat_id
        self._owner_ids = tuple(owner_ids)
        self._attendance_ui: AttendanceUI | None = None

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
                await asyncio.sleep(min(exc.retry_after, MAX_RETRY_AFTER))
            except TelegramAPIError as exc:
                logger.warning("личное сообщение не доставлено (%s): %s", user_id, exc.message)
                return False
            else:
                return True
        return False

    async def send_service(self, text: str) -> bool:
        delivered = True
        for chunk in split_text(text):
            if not await self._send_service_chunk(chunk):
                delivered = False
        return delivered

    async def _send_service_chunk(self, chunk: str) -> bool:
        try:
            try:
                await self._bot.send_message(self._service_chat_id, chunk)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, MAX_RETRY_AFTER))
                await self._bot.send_message(self._service_chat_id, chunk)
        except TelegramMigrateToChat as exc:
            logger.error(
                "служебный чат переехал: migrate_to_chat_id=%s; укажите SERVICE_CHAT_ID=%s в .env",
                exc.migrate_to_chat_id,
                exc.migrate_to_chat_id,
            )
            hint = SERVICE_CHAT_MIGRATED_HINT.format(chat_id=exc.migrate_to_chat_id)
            await self._duplicate_to_owners(chunk, hint)
        except TelegramForbiddenError as exc:
            logger.error("служебный чат недоступен: %s", exc.message)
            await self._duplicate_to_owners(chunk)
        except TelegramBadRequest as exc:
            if _CHAT_NOT_FOUND in exc.message.lower():
                logger.error("служебный чат не найден: проверьте SERVICE_CHAT_ID")
                await self._duplicate_to_owners(chunk)
            else:
                logger.warning("служебное сообщение не доставлено: %s", exc.message)
        except TelegramAPIError as exc:
            logger.warning("служебное сообщение не доставлено: %s", exc.message)
        else:
            return True
        return False

    async def _duplicate_to_owners(self, chunk: str, hint: str = "") -> None:
        """Служебная информация не должна пропадать вместе с недоступным чатом."""
        header = f"{SERVICE_CHAT_UNAVAILABLE_TEXT}\n{hint}".rstrip("\n")
        for part in split_text(f"{header}\n\n{chunk}"):
            for owner_id in self._owner_ids:
                await self.send_dm(owner_id, part)

    async def send_service_document(self, path: Path) -> None:
        """Отправляет файл в служебный чат; ошибки Telegram пробрасываются вызывающему."""
        size = (await asyncio.to_thread(path.stat)).st_size
        if size > UPLOAD_LIMIT_BYTES:
            logger.warning("файл %s (%s байт) больше лимита Telegram, не отправлен", path, size)
            return
        await self._bot.send_document(
            self._service_chat_id,
            FSInputFile(path),
            caption=BACKUP_CAPTION.format(name=path.name),
        )

    def attach_attendance_ui(self, ui: AttendanceUI) -> None:
        """Привязка поздняя: интерфейсу явки нужны сервисы, а сервисам — уведомитель."""
        self._attendance_ui = ui

    async def send_attendance_prompt(self, admin_id: int, event_id: int) -> bool:
        if self._attendance_ui is None:
            logger.warning("интерфейс явки не привязан (event=%s)", event_id)
            return False
        return await self._attendance_ui.send_sheet(admin_id, event_id) == SheetStatus.SENT
