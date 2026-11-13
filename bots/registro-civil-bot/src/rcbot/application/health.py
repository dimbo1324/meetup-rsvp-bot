"""Здоровье бота (SPEC 2.4, 6.1): ежедневный отчёт в Telegram и пинг внешнего «сторожа»."""

from __future__ import annotations

import logging
import statistics
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rcbot.application.messages import MessageComposer
from rcbot.application.ports import Clock, IncidentRepository, StatsRepository, WatchdogPinger
from rcbot.domain.models import Incident, PollStatsHour

logger = logging.getLogger(__name__)

WATCHDOG_EVERY_SEC = 60.0
MEDIAN_Q = 0.5
TAIL_Q = 0.95
# Инциденты за сутки читаются страницами: первая — столько, дальше вдвое больше
INCIDENTS_PAGE = 50


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
    # Сырые задержки прошлых часов в БД не хранятся: сводим часовые перцентили (медиана медиан,
    # худший 95-й), а не пересчитываем по всем проверкам суток
    p50 = [h.percentile(MEDIAN_Q) for h in hours if h.checks]
    p95 = [h.percentile(TAIL_Q) for h in hours if h.checks]
    return MessageComposer.daily_report(
        checks=checks,
        errors=errors,
        relogins=relogins,
        median_ms=statistics.median(p50) if p50 else 0.0,
        p95_ms=max(p95) if p95 else 0.0,
        incidents=incidents_24h,
        size_bytes=size_bytes,
    )


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
        # Строки статистики помечены началом часа, а отчёт уходит чуть позже ровного часа:
        # без округления вниз сутки потеряли бы свой первый час
        window = (now - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
        hours = await self._stats.since(window)
        if (current := self._current_hour()) is not None:
            hours.append(current)
        incidents = await self._incidents_since(now - timedelta(days=1))
        return report_text(hours, len(incidents), self._folder_size())

    async def _incidents_since(self, since: datetime) -> list[Incident]:
        limit = INCIDENTS_PAGE
        while True:
            found = await self._incidents.last(limit)
            # Список идёт от новых к старым: если самый старый ещё внутри суток, запрашиваем больше
            if len(found) < limit or found[-1].opened_at < since:
                return [i for i in found if i.opened_at >= since]
            limit *= 2

    async def daily_reports(self) -> None:
        while True:
            now = self._clock.now()
            due = next_report_at(now, self._tz, self._hour)
            await self._clock.sleep((due - now).total_seconds())
            try:
                await self._send(await self.build_report())
            except Exception:  # один неудавшийся отчёт не должен останавливать отчёты навсегда
                logger.exception("ежедневный отчёт не отправлен")

    async def watchdog(self) -> None:
        """Пустой запрос раз в минуту; если запросов нет 5 минут, сервис сам напишет владельцу."""
        if self._watchdog is None:
            return
        while True:
            try:
                if not await self._watchdog.ping():
                    logger.info("«сторож» не ответил")
            except Exception:  # сбой пинга не должен останавливать сторожа
                logger.exception("пинг «сторожа» упал")
            await self._clock.sleep(WATCHDOG_EVERY_SEC)
