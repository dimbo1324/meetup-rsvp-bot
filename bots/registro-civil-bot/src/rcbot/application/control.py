"""Фасад управления для Telegram и CLI (SPEC 2.5): значения, а тексты — в `bot/texts.py`."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from rcbot.application.alerting import AlertOrchestrator
from rcbot.application.incidents import IncidentBook
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import Clock, EventLog, IncidentRepository, Journal, NullJournal
from rcbot.application.session_manager import SessionManager
from rcbot.application.status import PollStatus
from rcbot.config.settings import MIN_POLL_INTERVAL_SEC
from rcbot.domain.errors import RcbotError
from rcbot.domain.models import AppEvent, Channel, DeliveryResult, Incident, SourceKind

FOREVER = timedelta(days=3650)
MAX_PAUSE_MINUTES = 525_600  # год


class IntervalTooSmall(ValueError):
    """Интервал меньше жёсткого минимума: не применяется (критерий приёмки 9)."""


@dataclass(frozen=True)
class StatusView:
    status: PollStatus
    source: SourceKind
    interval_sec: float
    base_interval_sec: float
    months_ahead: int
    paused_until: datetime | None
    incident: Incident | None
    repeating: bool
    logins_last_hour: int
    quiet_remaining_sec: float
    channels_off: frozenset[Channel]
    folder_bytes: int | None
    journal_bytes: int | None
    hung_reason: str
    now: datetime


@dataclass(frozen=True)
class IncidentView:
    incident: Incident
    deliveries: list[DeliveryResult]


class Controller:
    def __init__(
        self,
        loop: PollLoop,
        alerts: AlertOrchestrator,
        book: IncidentBook,
        incidents: IncidentRepository,
        event_log: EventLog,
        session: SessionManager,
        clock: Clock,
        cleanup: Callable[[], Awaitable[dict[str, int]]] | None = None,
        folder_size: Callable[[], int] | None = None,
        journal: Journal | None = None,
        journal_size: Callable[[], int] | None = None,
        liveness: Callable[[], str] | None = None,
    ) -> None:
        self._loop = loop
        self._alerts = alerts
        self._book = book
        self._incidents = incidents
        self._event_log = event_log
        self._session = session
        self._clock = clock
        self._cleanup = cleanup
        self._folder_size = folder_size
        self._journal: Journal = journal or NullJournal()
        self._journal_size = journal_size
        self._liveness = liveness

    async def status(self) -> StatusView:
        now = self._clock.now()
        runtime = self._loop.runtime
        return StatusView(
            status=self._loop.status,
            source=self._loop.source_kind,
            interval_sec=self._loop.current_interval(now),
            base_interval_sec=runtime.base_interval_sec,
            months_ahead=runtime.months_ahead,
            paused_until=runtime.paused_until if runtime.paused(now) else None,
            incident=await self._book.current(),
            repeating=self._alerts.repeating(),
            logins_last_hour=self._session.logins_last_hour(),
            quiet_remaining_sec=self._session.quiet_remaining(),
            channels_off=frozenset(runtime.channels_off),
            folder_bytes=self._folder_size() if self._folder_size else None,
            journal_bytes=self._journal_size() if self._journal_size else None,
            hung_reason=self._liveness() if self._liveness else "",
            now=now,
        )

    async def pause(self, minutes: int | None) -> datetime:
        if minutes is not None and not 0 <= minutes <= MAX_PAUSE_MINUTES:
            # timedelta(minutes=10**12) падает OverflowError: отказ должен быть понятным
            raise ValueError(
                f"пауза от 1 минуты до {MAX_PAUSE_MINUTES} (год); без числа — до /resume"
            )
        until = self._clock.now() + (timedelta(minutes=minutes) if minutes else FOREVER)
        await self._loop.pause(until)
        self._journal.event("command.pause", f"пауза до {until.isoformat()}")
        return until

    async def resume(self) -> None:
        await self._loop.resume()
        self._journal.event("command.resume", "опрос продолжен")

    async def set_interval(self, seconds: float) -> float:
        # NaN не меньше минимума ни в одном сравнении: проверяем число отдельно
        if not math.isfinite(seconds) or seconds < MIN_POLL_INTERVAL_SEC:
            raise IntervalTooSmall(f"меньше {MIN_POLL_INTERVAL_SEC:.0f} с или не число нельзя")
        value = await self._loop.set_interval(seconds)
        self._journal.event("command.interval", f"базовый интервал {value:.0f} с")
        return value

    async def set_months(self, months: int) -> int:
        value = await self._loop.set_months(months)
        self._journal.event("command.months", f"месяцев вперёд: {value}")
        return value

    async def toggle_channel(self, channel: Channel) -> bool:
        runtime = self._loop.runtime
        if channel in runtime.channels_off:
            runtime.channels_off.discard(channel)
        else:
            runtime.channels_off.add(channel)
        await self._loop.save_runtime()
        enabled = channel not in runtime.channels_off
        self._journal.event("command.channel", f"{channel}: {'включён' if enabled else 'выключен'}")
        return enabled

    async def test(self, channel: Channel | None = None) -> list[DeliveryResult]:
        self._journal.event("command.test", f"тест каналов: {channel or 'все'}")
        return await self._alerts.test({channel} if channel else None)

    async def ack(self) -> bool:
        return await self._alerts.ack()

    def manual_done(self) -> None:
        self._journal.event("command.manual_done", "владелец прошёл шаг 1")
        self._loop.manual_done()

    async def relogin(self) -> str:
        self._journal.event("command.relogin", "повторный вход по команде")
        try:
            await self._loop.relogin()
        except RcbotError as error:
            return str(error)
        return ""

    async def cleanup(self) -> dict[str, int]:
        removed = await self._cleanup() if self._cleanup else {}
        self._journal.event("command.cleanup", f"очистка: {removed}")
        return removed

    async def last(self, limit: int = 5) -> list[IncidentView]:
        incidents = await self._incidents.last(limit)
        return [IncidentView(i, await self._incidents.deliveries_for(i.id)) for i in incidents]

    async def recent_errors(self, limit: int = 5) -> list[AppEvent]:
        return await self._event_log.last(limit, "WARNING")
