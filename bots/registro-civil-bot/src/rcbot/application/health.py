"""Здоровье бота (SPEC 2.4, 6.1): ежедневный отчёт в Telegram и пинг внешнего «сторожа»."""

from __future__ import annotations

import logging
import statistics
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rcbot.application.ports import Clock, IncidentRepository, StatsRepository, WatchdogPinger
from rcbot.domain.models import PollStatsHour

logger = logging.getLogger(__name__)

WATCHDOG_EVERY_SEC = 60.0


def next_report_at(now: datetime, tz: ZoneInfo, hour: int) -> datetime:
    local = now.astimezone(tz)
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target


def report_text(hours: list[PollStatsHour], incidents_24h: int, size_bytes: int | None) -> str:
    checks = sum(h.checks for h in hours)
    errors = sum(h.errors for h in hours)
    relogins = sum(h.relogins for h in hours)
    p50 = [h.percentile(0.5) for h in hours if h.checks]
    p95 = [h.percentile(0.95) for h in hours if h.checks]
    lines = [
        "📋 Отчёт rcbot за сутки",
        f"Проверок: {checks}, ошибок: {errors}, входов на сайт: {relogins}",
        f"Время ответа: медиана {statistics.median(p50) if p50 else 0:.0f} мс, "
        f"95-й перцентиль {max(p95) if p95 else 0:.0f} мс",
        f"Инцидентов: {incidents_24h}",
    ]
    if size_bytes is not None:
        lines.append(f"Размер папки: {size_bytes / 1024 / 1024:.0f} МБ")
    if checks == 0:
        lines.append("⚠️ За сутки не было ни одной успешной проверки.")
    return "\n".join(lines)


class HealthMonitor:
    def __init__(
        self,
        stats: StatsRepository,
        incidents: IncidentRepository,
        clock: Clock,
        tz: ZoneInfo,
        report_hour: int,
        send: Callable[[str], Awaitable[None]],
        current_hour: Callable[[], PollStatsHour | None],
        folder_size: Callable[[], int],
        watchdog: WatchdogPinger | None,
    ) -> None:
        self._stats = stats
        self._incidents = incidents
        self._clock = clock
        self._tz = tz
        self._hour = report_hour
        self._send = send
        self._current_hour = current_hour
        self._folder_size = folder_size
        self._watchdog = watchdog

    async def build_report(self) -> str:
        now = self._clock.now()
        hours = await self._stats.since(now - timedelta(hours=24))
        if (current := self._current_hour()) is not None:
            hours.append(current)
        incidents = [
            i for i in await self._incidents.last(50) if i.opened_at >= now - timedelta(days=1)
        ]
        return report_text(hours, len(incidents), self._folder_size())

    async def daily_reports(self) -> None:
        while True:
            now = self._clock.now()
            due = next_report_at(now, self._tz, self._hour)
            await self._clock.sleep((due - now).total_seconds())
            await self._send(await self.build_report())

    async def watchdog(self) -> None:
        """Пустой запрос раз в минуту; если запросов нет 5 минут, сервис сам напишет владельцу."""
        if self._watchdog is None:
            return
        while True:
            if not await self._watchdog.ping():
                logger.info("«сторож» не ответил")
            await self._clock.sleep(WATCHDOG_EVERY_SEC)
