"""Цикл планировщика: раз в минуту прогоняет все задачи и пишет файл-пульс (17.6)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.scheduler.tasks import ScheduledTask

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT = Path("data/heartbeat")


class Scheduler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        tasks: Sequence[ScheduledTask],
        interval: float = 60.0,
        heartbeat_path: Path = DEFAULT_HEARTBEAT,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._tasks = tasks
        self._interval = interval
        self._heartbeat_path = heartbeat_path

    async def tick(self) -> dict[str, int]:
        """Один прогон задач по порядку; ошибка одной не мешает остальным.

        Возвращает {имя задачи: число обработанных}; упавшая задача в результат не попадает.
        """
        now = self._clock.now()
        done: dict[str, int] = {}
        for task in self._tasks:
            try:
                count = await task.run(now)
            except Exception:
                logger.exception("задача планировщика %s завершилась ошибкой", task.name)
                continue
            done[task.name] = count
            if count:
                logger.info("задача %s: обработано %d", task.name, count)
        await self._write_heartbeat(now)
        return done

    async def run_forever(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    # tick сам ловит ошибки задач; сюда попадёт лишь сбой самого цикла
                    logger.exception("сбой тика планировщика")
                await asyncio.sleep(self._seconds_to_next_boundary())
        except asyncio.CancelledError:
            logger.info("планировщик остановлен")
            raise

    def _seconds_to_next_boundary(self) -> float:
        # Просыпаемся в начале следующей минуты, а не через фиксированный интервал после тика:
        # так время срабатывания не «плывёт» на длительность самих задач
        elapsed = self._clock.now().timestamp() % self._interval
        return self._interval - elapsed

    async def _write_heartbeat(self, now: datetime) -> None:
        try:
            await asyncio.to_thread(self._write_heartbeat_sync, now)
        except OSError:
            logger.exception("не удалось записать файл-пульс %s", self._heartbeat_path)

    def _write_heartbeat_sync(self, now: datetime) -> None:
        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        # Healthcheck смотрит на mtime файла; содержимое нужно только человеку
        self._heartbeat_path.write_text(now.isoformat(), encoding="utf-8")
