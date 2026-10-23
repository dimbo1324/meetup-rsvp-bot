"""Фоновая правка inline-сообщений встречи с debounce и обработкой лимитов Telegram."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.renderer.post import RenderedPost
from meetup_bot.repositories import event_posts
from meetup_bot.services.settings import SettingsService
from meetup_bot.telegram.keyboards import to_markup
from meetup_bot.telegram.posts import build_post

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# Верхняя граница ожидания при лимитах и сбоях сети, чтобы правка не зависала надолго
MAX_RETRY_AFTER = 30.0

_NOT_MODIFIED = "message is not modified"
# Сообщение недоступно навсегда: пост исчез, истёк или не принадлежит боту
_DEAD_MARKERS = (
    "message_id_invalid",
    "message to edit not found",
    "message can't be edited",
)
# Тип поста (фото/текст) изменился после рассылки: правим другим методом
_WRONG_KIND_MARKERS = ("there is no caption in the message", "there is no text in the message")


class PostUpdater:
    def __init__(
        self,
        bot: Bot,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        settings: SettingsService,
        *,
        debounce: float = 1.5,
        pause: float = 0.05,
    ) -> None:
        self._bot = bot
        self._sessions = sessions
        self._clock = clock
        self._settings = settings
        self._debounce = debounce
        self._pause = pause
        self._tasks: dict[int, asyncio.Task[None]] = {}
        # Запросы, пришедшие во время уже идущей правки: после неё нужен ещё один проход
        self._rerun: set[int] = set()
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def schedule(self, event_id: int) -> None:
        """Синхронно и без исключений: вызывается из сервисов после коммита."""
        task = self._tasks.get(event_id)
        if task is not None and not task.done():
            # Задача ещё ждёт debounce — она подхватит свежие данные сама. Если уже
            # правит, нужен повторный проход, иначе изменение потеряется.
            self._rerun.add(event_id)
            return
        try:
            self._tasks[event_id] = asyncio.get_running_loop().create_task(self._run(event_id))
        except RuntimeError:
            logger.warning("цикл событий не запущен: пересборка постов %s пропущена", event_id)

    async def aclose(self, wait: float = 5.0) -> None:
        # Дожидаемся уже запланированных правок вместо отмены: очередь живёт только в памяти
        self._debounce = 0.0
        tasks = list(self._tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=wait)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._rerun.clear()

    async def _run(self, event_id: int) -> None:
        try:
            await asyncio.sleep(self._debounce)
            while True:
                self._rerun.discard(event_id)
                try:
                    async with self._locks[event_id]:
                        await self._refresh(event_id)
                except Exception:
                    logger.exception("не удалось обновить посты встречи %s", event_id)
                if event_id not in self._rerun:
                    break
        finally:
            # Удаляем себя только если мы всё ещё актуальная задача
            if self._tasks.get(event_id) is asyncio.current_task():
                del self._tasks[event_id]
                self._rerun.discard(event_id)
                self._locks.pop(event_id, None)

    async def _refresh(self, event_id: int) -> None:
        built = await build_post(self._sessions, self._settings, event_id)
        if built is None:
            return
        rendered = built[1]
        async with self._sessions() as session:
            posts = await event_posts.list_live(session, event_id)
        # Дальше только простые значения: ORM-объекты после закрытия сессии не трогаем
        targets = [(p.id, p.inline_message_id, p.last_render_hash) for p in posts]
        first = True
        for post_id, inline_id, last_hash in targets:
            if inline_id is None or last_hash == rendered.content_hash:
                continue
            if not first:
                await asyncio.sleep(self._pause)
            first = False
            try:
                await self._edit_one(post_id, inline_id, rendered)
            except Exception:
                logger.exception("ошибка правки поста %s встречи %s", post_id, event_id)

    async def _edit_one(self, post_id: int, inline_id: str, rendered: RenderedPost) -> None:
        for attempt in range(MAX_ATTEMPTS):
            try:
                await self._send_edit(inline_id, rendered)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, MAX_RETRY_AFTER))
                continue
            except TelegramNetworkError, TelegramServerError:
                # Сеть или 5xx: ограниченный повтор с нарастающей паузой
                logger.warning("временный сбой Telegram при правке поста %s", post_id)
                await asyncio.sleep(min(2**attempt, MAX_RETRY_AFTER))
                continue
            except TelegramBadRequest as exc:
                message = exc.message.lower()
                if _NOT_MODIFIED in message:
                    break  # содержимое уже такое — считаем успехом
                if any(marker in message for marker in _WRONG_KIND_MARKERS):
                    rendered = dataclasses.replace(rendered, has_photo=not rendered.has_photo)
                    continue
                if any(marker in message for marker in _DEAD_MARKERS):
                    await self._mark_dead(post_id)
                    return
                logger.error("Telegram отклонил правку поста %s: %s", post_id, exc.message)
                return
            except TelegramForbiddenError:
                await self._mark_dead(post_id)
                return
            except TelegramAPIError:
                logger.exception("сбой Telegram при правке поста %s", post_id)
                return
            else:
                break
        else:
            logger.warning("правка поста %s: исчерпаны попытки из-за лимитов", post_id)
            return
        async with self._sessions() as session, session.begin():
            await event_posts.mark_edited(
                session, post_id, content_hash=rendered.content_hash, now=self._clock.now()
            )

    async def _send_edit(self, inline_id: str, rendered: RenderedPost) -> None:
        # Пустая разметка снимает кнопки; None оставил бы прежние
        markup = to_markup(rendered) or InlineKeyboardMarkup(inline_keyboard=[])
        if rendered.has_photo:
            await self._bot.edit_message_caption(
                inline_message_id=inline_id, caption=rendered.text, reply_markup=markup
            )
        else:
            await self._bot.edit_message_text(
                text=rendered.text,
                inline_message_id=inline_id,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    async def _mark_dead(self, post_id: int) -> None:
        async with self._sessions() as session, session.begin():
            await event_posts.mark_dead(session, post_id)
