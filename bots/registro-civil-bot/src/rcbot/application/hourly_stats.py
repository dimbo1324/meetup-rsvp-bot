"""Почасовая статистика проверок (SPEC 2.2, 2.7): в БД идёт только итог часа, не каждая проверка."""

from __future__ import annotations

import logging

from rcbot.application.background import BackgroundTasks
from rcbot.application.ports import Clock, StatsRepository
from rcbot.domain.models import PollStatsHour

logger = logging.getLogger(__name__)


class HourlyStats:
    def __init__(
        self, clock: Clock, repository: StatsRepository, background: BackgroundTasks
    ) -> None:
        self._clock = clock
        self._repository = repository
        self._background = background
        self.current: PollStatsHour | None = None

    def count(
        self, *, latency_ms: float | None = None, error: bool = False, relogins: int = 0
    ) -> None:
        hour = self._clock.now().replace(minute=0, second=0, microsecond=0)
        if self.current is not None and self.current.hour != hour:
            # Закрытый час сохраняется фоном: запись в БД не задерживает проверки
            self._background.spawn(self._repository.save_hour(self.current))
            self.current = None
        if self.current is None:
            self.current = PollStatsHour(hour)
        if latency_ms is not None:
            self.current.checks += 1
            self.current.latencies_ms.append(latency_ms)
        self.current.errors += int(error)
        self.current.relogins += relogins

    async def flush(self) -> None:
        if self.current is None:
            return
        try:
            await self._repository.save_hour(self.current)
        except Exception as error:  # остановку не задерживаем из-за сбоя записи статистики
            logger.warning("статистика часа не сохранена: %s", type(error).__name__)
