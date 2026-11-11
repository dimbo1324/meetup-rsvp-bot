"""Ограничение частоты: защита от зажатой кнопки и флуда в личке (раздел 8, безопасность)."""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware, Dispatcher
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from meetup_bot.clock import Clock
from meetup_bot.telegram.common import safe_answer

RATE_LIMIT_EVENTS = 5
RATE_LIMIT_WINDOW = 2.0
# Память под счётчики не растёт бесконечно: самые давние люди вытесняются первыми
MAX_TRACKED_USERS = 10_000
RATE_LIMIT_TEXT = "Слишком часто, подождите секунду."


class RateLimiter:
    """Скользящее окно в памяти: не больше `events` событий за `RATE_LIMIT_WINDOW` на человека."""

    def __init__(
        self, clock: Clock, *, events: int = RATE_LIMIT_EVENTS, max_users: int = MAX_TRACKED_USERS
    ) -> None:
        self._clock = clock
        self._events = events
        self._max_users = max_users
        self._window = timedelta(seconds=RATE_LIMIT_WINDOW)
        self._hits: OrderedDict[int, deque[datetime]] = OrderedDict()

    @property
    def tracked(self) -> int:
        return len(self._hits)

    def allow(self, user_id: int) -> bool:
        now = self._clock.now()
        hits = self._hits.pop(user_id, None)
        if hits is None:
            hits = deque()
        # Отметки старше окна забываются: это и есть TTL
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        allowed = len(hits) < self._events
        if allowed:
            hits.append(now)
        if hits:
            self._hits[user_id] = hits
        while len(self._hits) > self._max_users:
            self._hits.popitem(last=False)
        return allowed


class RateLimitMiddleware(BaseMiddleware):
    """Нажатия сверх лимита получают ответ, сообщения в личке молча игнорируются.

    Админы не освобождаются: лимит защищает бота и от скомпрометированной учётной записи.
    """

    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or (isinstance(event, Message) and event.chat.type != ChatType.PRIVATE):
            return await handler(event, data)
        if self._limiter.allow(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await safe_answer(event, RATE_LIMIT_TEXT, show_alert=False)
        return None


def install_rate_limit(dispatcher: Dispatcher, clock: Clock) -> None:
    """Подключает ограничение на уровне диспетчера: оно срабатывает до роутеров и БД."""
    middleware = RateLimitMiddleware(RateLimiter(clock))
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
