"""EventBus в памяти (SPEC 2.3): публикация не ждёт подписчиков — каждый работает своей задачей.

Так рассылка тревоги не ждёт записи в БД, а сбой одного подписчика не задевает остальных.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from rcbot.domain.events import DomainEvent

logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[tuple[type[DomainEvent], Handler]] = []
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_type: type[DomainEvent], handler: Handler) -> None:
        self._handlers.append((event_type, handler))

    def publish(self, event: DomainEvent) -> None:
        for event_type, handler in self._handlers:
            if isinstance(event, event_type):
                task = asyncio.create_task(self._run(handler, event))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _run(handler: Handler, event: DomainEvent) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("подписчик %s упал на событии %s", handler, type(event).__name__)

    async def drain(self, wait_sec: float = 10.0) -> None:
        """Дождаться начатой работы подписчиков при остановке."""
        if self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=wait_sec)
            for task in pending:
                task.cancel()
            # Отмену доводим до конца: задача не остаётся висеть «наполовину отменённой»
            await asyncio.gather(*pending, return_exceptions=True)
