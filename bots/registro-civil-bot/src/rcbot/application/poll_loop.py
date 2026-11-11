"""Цикл опроса (SPEC 2.2–2.3): расписание без дрейфа, состояния, отступление, смена источника.

Следующая проверка считается от `loop.time()` предыдущего срока с разбросом ±jitter, а не
«сон после окончания». Ошибки сайта замедляют опрос, а не учащают его (SPEC 0.3, правило 4).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Coroutine
from dataclasses import replace
from datetime import datetime
from typing import Any

from rcbot.application.bus import EventBus
from rcbot.application.ports import AvailabilitySource, Clock, EventLog, StatsRepository
from rcbot.application.runtime import RuntimeSettings, RuntimeStore, clamp_interval
from rcbot.application.session_manager import SessionManager
from rcbot.application.status import PollStatus
from rcbot.config.settings import MIN_DOM_POLL_INTERVAL_SEC, MIN_POLL_INTERVAL_SEC
from rcbot.domain.detector import Detector
from rcbot.domain.errors import (
    HostNotAllowed,
    LoginFailed,
    ManualStepRequired,
    RateLimited,
    ReloginBudgetExceeded,
    ReloginDeferred,
    SessionExpired,
    SiteUnavailable,
    SourceNotConfigured,
    UnexpectedResponse,
)
from rcbot.domain.events import ManualActionRequired, PollerStateChanged, SourceSwitched
from rcbot.domain.models import AppEvent, PollState, PollStatsHour, SourceKind, WatchTarget

logger = logging.getLogger(__name__)

BACKOFF_START_SEC = 30.0
BACKOFF_MAX_SEC = 900.0
RATE_LIMIT_PAUSE_SEC = 900.0
MANUAL_RECHECK_SEC = 900.0
REAUTH_RETRY_SEC = 30.0
REAUTH_FAILURES_TO_BACKOFF = 3
UNEXPECTED_TO_SWITCH = 3


class PollLoop:
    def __init__(
        self,
        *,
        primary: AvailabilitySource,
        fallback: AvailabilitySource | None,
        session: SessionManager,
        detector: Detector,
        bus: EventBus,
        clock: Clock,
        runtime: RuntimeSettings,
        runtime_store: RuntimeStore,
        stats: StatsRepository,
        journal: EventLog,
        target: WatchTarget,
        jitter: float,
        rng: random.Random | None = None,
    ) -> None:
        self._source = primary
        self._fallback = fallback
        self._session = session
        self._detector = detector
        self._bus = bus
        self._clock = clock
        self.runtime = runtime
        self._runtime_store = runtime_store
        self._stats = stats
        self._journal = journal
        self._target = target
        self._jitter = jitter
        self._rng = rng or random.Random()
        self.status = PollStatus(started_at=clock.now(), source=primary.kind)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._background: set[asyncio.Task[None]] = set()
        self._next_due = 0.0
        self._backoff_step = 0
        self._reauth_failures = 0
        self._unexpected_streak = 0
        self._hour: PollStatsHour | None = None

    # --- управление (Telegram, CLI) ---

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()

    async def pause(self, until: datetime | None) -> None:
        self.runtime.paused_until = until
        await self._runtime_store.save(self.runtime)
        self.wake()

    async def resume(self) -> None:
        self.runtime.paused_until = None
        await self._runtime_store.save(self.runtime)
        self.wake()

    async def set_interval(self, seconds: float) -> float:
        self.runtime.base_interval_sec = clamp_interval(seconds)
        await self._runtime_store.save(self.runtime)
        self.wake()
        return self.runtime.base_interval_sec

    async def set_months(self, months: int) -> int:
        self.runtime.months_ahead = max(1, min(12, months))
        await self._runtime_store.save(self.runtime)
        return self.runtime.months_ahead

    def manual_done(self) -> None:
        """Владелец прошёл шаг 1 сам и нажал «Готово» (D3): проверка — сразу."""
        self.wake()

    async def relogin(self) -> None:
        self._session.mark_expired()
        await self._session.relogin("по команде владельца", force=True)
        self.wake()

    def current_interval(self, now: datetime) -> float:
        interval = self.runtime.ladder_interval(now)
        if self._source.kind is SourceKind.DOM:
            interval = max(interval, MIN_DOM_POLL_INTERVAL_SEC)
        return clamp_interval(interval)

    # --- цикл ---

    async def run(self) -> None:
        self._next_due = self._clock.monotonic()
        while not self._stop.is_set():
            now = self._clock.now()
            if self.runtime.paused(now):
                self._set_state(PollState.PAUSED, "пауза по команде")
                until = self.runtime.paused_until
                await self._wait((until - now).total_seconds() if until else BACKOFF_MAX_SEC)
                continue
            if self.status.state is PollState.PAUSED:
                self._set_state(PollState.RUNNING, "пауза закончилась")
                self._next_due = self._clock.monotonic()
            delay = await self._tick()
            self.status.next_check_in_sec = delay
            await self._wait(delay)
        await self._flush_hour()

    async def _wait(self, seconds: float) -> None:
        if self._wake.is_set() or self._stop.is_set():
            self._wake.clear()
            return
        sleeper = asyncio.ensure_future(self._clock.sleep(max(0.0, seconds)))
        waker = asyncio.ensure_future(self._wake.wait())
        stopper = asyncio.ensure_future(self._stop.wait())
        _, pending = await asyncio.wait(
            {sleeper, waker, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if self._wake.is_set():
            self._wake.clear()
            self._next_due = self._clock.monotonic()

    async def _tick(self) -> float:
        target = replace(self._target, months_ahead=self.runtime.months_ahead)
        logins_before = self._session.logins
        try:
            snapshot = await self._source.fetch(target)
        except SessionExpired as error:
            return await self._reauth(str(error))
        except ManualStepRequired as error:
            return self._needs_manual(str(error))
        except RateLimited as error:
            return await self._rate_limited(str(error))
        except UnexpectedResponse as error:
            return self._unexpected(str(error))
        except SourceNotConfigured as error:
            return self._switch_to_fallback(str(error)) or self._backoff(str(error))
        except (SiteUnavailable, HostNotAllowed) as error:
            return self._backoff(str(error))
        finally:
            if self._session.logins != logins_before:
                self._count(relogin=True)

        self._backoff_step = 0
        self._unexpected_streak = 0
        self._set_state(PollState.RUNNING, "проверка прошла")
        self.status.last_check_at = snapshot.observed_at
        self.status.last_latency_ms = snapshot.latency_ms
        self.status.checks_total += 1
        self.status.note_latency(snapshot.latency_ms)
        self._count(latency_ms=snapshot.latency_ms)
        event = self._detector.compare(snapshot)
        if event is not None:
            self._bus.publish(event)
        return self._schedule_next()

    def _schedule_next(self) -> float:
        now_mono = self._clock.monotonic()
        interval = self.current_interval(self._clock.now())
        self.status.interval_sec = interval
        factor = 1 + self._rng.uniform(-self._jitter, self._jitter)
        self._next_due += max(MIN_POLL_INTERVAL_SEC, interval * factor)
        if self._next_due < now_mono:
            # Проверка длилась дольше интервала: без «догоняющей» серии запросов
            self._next_due = now_mono + MIN_POLL_INTERVAL_SEC
        return self._next_due - now_mono

    def _delay(self, seconds: float) -> float:
        self._next_due = self._clock.monotonic() + seconds
        return seconds

    # --- ошибки ---

    async def _reauth(self, reason: str) -> float:
        self._set_state(PollState.REAUTH, reason)
        self._session.mark_expired()
        try:
            await self._session.relogin(reason)
        except ReloginDeferred:
            return self._delay(max(REAUTH_RETRY_SEC, self._session.quiet_remaining()))
        except ReloginBudgetExceeded as error:
            self._set_state(PollState.BACKOFF, str(error))
            return self._delay(BACKOFF_MAX_SEC)
        except (LoginFailed, SiteUnavailable, RateLimited) as error:
            self._reauth_failures += 1
            self._error(f"вход не удался: {error}")
            if self._reauth_failures >= REAUTH_FAILURES_TO_BACKOFF:
                self._set_state(
                    PollState.BACKOFF, f"{self._reauth_failures} неудачных входов подряд"
                )
                self._backoff_step += 1
                return self._delay(self._backoff_delay())
            return self._delay(REAUTH_RETRY_SEC)
        self._reauth_failures = 0
        self._set_state(PollState.RUNNING, "вход выполнен")
        return self._delay(0.0)

    def _needs_manual(self, reason: str) -> float:
        if self.status.state is not PollState.NEEDS_MANUAL:
            self._set_state(PollState.NEEDS_MANUAL, reason)
            self._bus.publish(ManualActionRequired(self._clock.now(), reason))
        # Если владелец пройдёт шаг 1 и не нажмёт «Готово», бот сам заметит это через 15 минут
        return self._delay(MANUAL_RECHECK_SEC)

    async def _rate_limited(self, reason: str) -> float:
        self._error(reason)
        self.runtime.step_back(self._clock.now())
        await self._runtime_store.save(self.runtime)
        self._set_state(PollState.BACKOFF, f"сайт ограничил запросы: {reason}; пауза 15 минут")
        return self._delay(RATE_LIMIT_PAUSE_SEC)

    def _unexpected(self, reason: str) -> float:
        self._unexpected_streak += 1
        if (
            self._source.kind is SourceKind.API
            and self._unexpected_streak >= UNEXPECTED_TO_SWITCH
            and self._switch_to_fallback(f"{self._unexpected_streak} неожиданных ответа подряд")
        ):
            return self._delay(0.0)
        return self._backoff(reason)

    def _switch_to_fallback(self, reason: str) -> bool:
        if self._fallback is None or self._fallback is self._source:
            return False
        old = self._source
        self._source, self._fallback = self._fallback, None
        self._unexpected_streak = 0
        self.status.source = self._source.kind
        self._bus.publish(SourceSwitched(self._clock.now(), old.kind, self._source.kind, reason))
        self._spawn(old.close())
        logger.warning("источник %s → %s: %s", old.kind, self._source.kind, reason)
        return True

    def _backoff(self, reason: str) -> float:
        self._error(reason)
        self._backoff_step += 1
        # Одиночный сбой сети — не повод беспокоить владельца: в BACKOFF со второго подряд
        if self._backoff_step >= 2:
            self._set_state(PollState.BACKOFF, reason)
        return self._delay(self._backoff_delay())

    def _backoff_delay(self) -> float:
        doublings = max(self._backoff_step - 1, 0)
        return min(BACKOFF_START_SEC * float(2**doublings), BACKOFF_MAX_SEC)

    # --- учёт ---

    def _set_state(self, state: PollState, reason: str) -> None:
        old = self.status.state
        if old is state:
            return
        self.status.state = state
        self.status.state_reason = reason
        self._bus.publish(PollerStateChanged(self._clock.now(), old, state, reason))
        level = "INFO" if state in (PollState.RUNNING, PollState.PAUSED) else "WARNING"
        self._spawn(
            self._journal.write(AppEvent(self._clock.now(), level, f"state.{state}", reason))
        )
        logger.info("состояние опроса %s → %s: %s", old, state, reason)

    def _error(self, reason: str) -> None:
        self.status.errors_total += 1
        self.status.last_error = reason
        self.status.last_error_at = self._clock.now()
        self._count(error=True)
        logger.warning("проверка не удалась: %s", reason)

    def _count(
        self, *, latency_ms: float | None = None, error: bool = False, relogin: bool = False
    ) -> None:
        now = self._clock.now()
        hour = now.replace(minute=0, second=0, microsecond=0)
        if self._hour is not None and self._hour.hour != hour:
            self._spawn(self._stats.save_hour(self._hour))
            self._hour = None
        if self._hour is None:
            self._hour = PollStatsHour(hour)
        if latency_ms is not None:
            self._hour.checks += 1
            self._hour.latencies_ms.append(latency_ms)
        self._hour.errors += int(error)
        self._hour.relogins += int(relogin)

    async def _flush_hour(self) -> None:
        if self._hour is not None:
            await self._stats.save_hour(self._hour)
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def current_hour(self) -> PollStatsHour | None:
        return self._hour

    @property
    def source_kind(self) -> SourceKind:
        return self._source.kind
