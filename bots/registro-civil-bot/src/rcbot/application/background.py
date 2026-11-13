"""Фоновые задачи компонента: ссылки держатся до завершения, упавшая задача попадает в лог.

asyncio хранит только слабые ссылки на задачи: без набора задача может быть собрана сборщиком
мусора посреди работы, а её исключение — потеряться молча.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._tasks: set[asyncio.Task[None]] = set()

    def __len__(self) -> int:
        return len(self._tasks)

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            # Только имя типа: в тексте исключения бывают адреса с номером заявки
            logger.warning("фоновая задача (%s) упала: %s", self._owner, type(error).__name__)

    async def drain(self, wait_sec: float | None = None) -> None:
        """Дождаться начатых задач; не успевшие за `wait_sec` отменяются и доводятся до конца."""
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=wait_sec)
        for task in pending:
            task.cancel()
        # Отмену доводим до конца: задача не остаётся висеть «наполовину отменённой»
        await asyncio.gather(*pending, return_exceptions=True)
