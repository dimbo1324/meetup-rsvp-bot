"""Супервизор фоновых задач: упавшая задача перезапускается с паузой, остановка — по порядку."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from rcbot.application.ports import Clock

logger = logging.getLogger(__name__)

RESTART_MIN_SEC = 5.0
RESTART_MAX_SEC = 300.0


class Supervisor:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._specs: dict[str, tuple[Callable[[], Awaitable[None]], bool]] = {}

    def start(
        self, name: str, factory: Callable[[], Awaitable[None]], *, restart: bool = True
    ) -> None:
        self._specs[name] = (factory, restart)
        self._tasks[name] = asyncio.create_task(self._guard(name, factory, restart), name=name)

    async def restart(self, name: str) -> None:
        """Принудительный перезапуск задачи (например, зависшего цикла опроса)."""
        task = self._tasks.get(name)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=10)
        factory, restart = self._specs[name]
        self.start(name, factory, restart=restart)

    async def _guard(
        self, name: str, factory: Callable[[], Awaitable[None]], restart: bool
    ) -> None:
        delay = RESTART_MIN_SEC
        while True:
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("задача «%s» упала", name)
                if not restart:
                    raise
            await self._clock.sleep(delay)
            delay = min(delay * 2, RESTART_MAX_SEC)
            logger.warning("задача «%s» перезапускается", name)

    async def stop(self, wait_sec: float = 10.0) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.wait(set(self._tasks.values()), timeout=wait_sec)
        self._tasks.clear()
