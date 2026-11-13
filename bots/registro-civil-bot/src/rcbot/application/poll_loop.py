"""Цикл опроса (SPEC 2.2–2.3): расписание без дрейфа, состояния, отступление, смена источника.

Следующая проверка считается от `loop.time()` предыдущего срока с разбросом ±jitter, а не
«сон после окончания». Ошибки сайта замедляют опрос, а не учащают его (SPEC 0.3, правило 4).
"""

from __future__ import annotations

import asyncio
import logging
import random
import traceback
from collections.abc import Coroutine
from dataclasses import replace
from datetime import datetime
from pathlib import PurePath
from typing import Any

from rcbot.application.bus import EventBus
from rcbot.application.ports import (
    AvailabilitySource,
    CheckRecord,
    Clock,
    EventLog,
    Journal,
    NullJournal,
    StatsRepository,
)
from rcbot.application.runtime import RuntimeSettings, RuntimeStore, clamp_interval
from rcbot.application.session_manager import SessionManager
from rcbot.application.status import PollStatus
from rcbot.domain.detector import Detector
from rcbot.domain.errors import (
    HostNotAllowed,
    ManualStepRequired,
    RateLimited,
    RcbotError,
    ReloginBudgetExceeded,
    ReloginDeferred,
    SessionExpired,
    SiteUnavailable,
    SourceNotConfigured,
    UnexpectedResponse,
)
from rcbot.domain.events import ManualActionRequired, PollerStateChanged, SourceSwitched
from rcbot.domain.limits import MIN_DOM_POLL_INTERVAL_SEC, MIN_POLL_INTERVAL_SEC
from rcbot.domain.models import (
    AppEvent,
    AvailabilitySnapshot,
    PollState,
    PollStatsHour,
    SourceKind,
    WatchTarget,
)

logger = logging.getLogger(__name__)

BACKOFF_START_SEC = 30.0
BACKOFF_MAX_SEC = 900.0
RATE_LIMIT_PAUSE_SEC = 900.0
MANUAL_RECHECK_SEC = 900.0
REAUTH_RETRY_SEC = 30.0
REAUTH_FAILURES_TO_BACKOFF = 3
UNEXPECTED_TO_SWITCH = 3
# Проверка дольше этого прерывается, браузер перезапускается (защита от зависания)
FETCH_TIMEOUT_SEC = 150.0


def _describe(error: BaseException) -> str:
    """Текст ошибки для журнала: свои исключения безопасны, чужие — только имя типа."""
    return str(error) if isinstance(error, RcbotError) else type(error).__name__


def _where(error: BaseException) -> str:
    """Где упало (файл:строка), без текста исключения: в нём бывают адреса и значения."""
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return "?"
    last = frames[-1]
    return f"{PurePath(last.filename).name}:{last.lineno} {last.name}"


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
        event_log: EventLog,
        target: WatchTarget,
        jitter: float,
        rng: random.Random | None = None,
        journal: Journal | None = None,
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
        self._event_log = event_log
        self._journal: Journal = journal or NullJournal()
        self._target = target
        self._jitter = jitter
        self._rng = rng or random.Random()
        self.status = PollStatus(started_at=clock.now(), source=primary.kind)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._background: set[asyncio.Task[None]] = set()
        self._next_due = 0.0
        # Не начинать проверку раньше этого момента: минимум между запросами (SPEC 0.3, правило 3)
        # держится при любых пробуждениях (/interval, «Готово», /resume, вход по команде)
        self._not_before = 0.0
        self._logins_counted = session.logins
        self._manual_confirmed = False
        self._backoff_step = 0
        self._reauth_failures = 0
        self._unexpected_streak = 0
        self._hour: PollStatsHour | None = None
        # Ход цикла для обнаружения зависаний (application/liveness.py)
        self.tick_started_at: float | None = None
        self.last_tick_finished_at: float | None = None

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

    async def save_runtime(self) -> None:
        await self._runtime_store.save(self.runtime)

    def manual_done(self) -> None:
        """Владелец прошёл шаг 1 сам и нажал «Готово» (D3): проверка — сразу."""
        self._manual_confirmed = True
        self.wake()

    async def relogin(self) -> None:
        """Вход по команде владельца. Отказ (пауза после тревоги, лимит) не трогает сессию."""
        try:
            await self._session.relogin("по команде владельца", force=True)
        finally:
            self._count_relogins()
        self.wake()

    def current_interval(self, now: datetime) -> float:
        interval = self.runtime.ladder_interval(now)
        if self._source.kind is SourceKind.DOM:
            interval = max(interval, MIN_DOM_POLL_INTERVAL_SEC)
        return clamp_interval(interval)

    # --- цикл ---

    async def run(self) -> None:
        self._next_due = self._clock.monotonic()
        try:
            while not self._stop.is_set():
                now = self._clock.now()
                if self.runtime.paused(now):
                    self._set_state(PollState.PAUSED, "пауза по команде")
                    until = self.runtime.paused_until
                    pause_sec = (until - now).total_seconds() if until else BACKOFF_MAX_SEC
                    self._next_due = self._clock.monotonic() + pause_sec
                    await self._wait(pause_sec)
                    continue
                if self.status.state is PollState.PAUSED:
                    self._set_state(PollState.RUNNING, "пауза закончилась")
                    self._next_due = self._clock.monotonic()
                await self._respect_minimum()
                if self._stop.is_set():
                    break
                try:
                    delay = await self._tick()
                except Exception as error:  # сбой самого цикла, не источника: опрос не рвём
                    delay = self._loop_crashed(error)
                self.status.next_check_in_sec = delay
                await self._wait(delay)
        finally:
            # Ещё и при отмене задачи (остановка, перезапуск): статистика часа не теряется
            await self._flush_hour()

    def _min_spacing(self) -> float:
        if self._source.kind is SourceKind.DOM:
            return MIN_DOM_POLL_INTERVAL_SEC
        return MIN_POLL_INTERVAL_SEC

    async def _respect_minimum(self) -> None:
        """Пробуждение по команде не сокращает минимум между проверками: ждём, прерываясь стопом."""
        remaining = self._not_before - self._clock.monotonic()
        if remaining <= 0:
            return
        sleeper = asyncio.ensure_future(self._clock.sleep(remaining))
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await self._cancel_all(sleeper, stopper)
        # Пробуждение, пришедшее за это время, исполняется проверкой, которая сейчас начнётся
        self._wake.clear()
        self._next_due = max(self._next_due, self._clock.monotonic())

    async def _wait(self, seconds: float) -> None:
        if self._wake.is_set() or self._stop.is_set():
            if self._wake.is_set():
                self._wake.clear()
                self._next_due = self._clock.monotonic()
            return
        sleeper = asyncio.ensure_future(self._clock.sleep(max(0.0, seconds)))
        waker = asyncio.ensure_future(self._wake.wait())
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({sleeper, waker, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Отмена run() во сне не оставляет висящих задач сна и ожидания событий
            await self._cancel_all(sleeper, waker, stopper)
        if self._wake.is_set():
            self._wake.clear()
            self._next_due = self._clock.monotonic()

    @staticmethod
    async def _cancel_all(*tasks: asyncio.Future[Any]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _tick(self) -> float:
        target = replace(self._target, months_ahead=self.runtime.months_ahead)
        started = self._clock.monotonic()
        self.tick_started_at = started
        self._not_before = started + self._min_spacing()
        try:
            async with asyncio.timeout(FETCH_TIMEOUT_SEC) as scope:
                snapshot = await self._source.fetch(target)
        except Exception as error:  # источник мог бросить что угодно: цикл не должен падать
            timed_out = isinstance(error, TimeoutError) and scope.expired()
            delay = await self._handle_error(error, timed_out=timed_out)
            self._record_check(None, (self._clock.monotonic() - started) * 1000, error)
            return delay
        finally:
            self.tick_started_at = None
            self.last_tick_finished_at = self._clock.monotonic()
            self._count_relogins()

        self._backoff_step = 0
        self._unexpected_streak = 0
        self._manual_confirmed = False
        self._set_state(PollState.RUNNING, "проверка прошла")
        self.status.last_check_at = snapshot.observed_at
        self.status.last_latency_ms = snapshot.latency_ms
        self.status.checks_total += 1
        self.status.note_latency(snapshot.latency_ms)
        self._count(latency_ms=snapshot.latency_ms)
        event = self._detector.compare(snapshot)
        if event is not None:
            self._bus.publish(event)
        delay = self._schedule_next()
        self._record_check(snapshot, snapshot.latency_ms, None)
        return delay

    async def _handle_error(self, error: Exception, *, timed_out: bool = False) -> float:
        if timed_out:
            # Зависшая проверка: браузер или соединение закрываются и создаются заново
            reason = f"проверка не уложилась в {FETCH_TIMEOUT_SEC:g} с и прервана"
            self._journal.event("check.timeout", reason, "WARNING")
            self._spawn(self.reset_source())
            return self._backoff(reason)
        if not isinstance(error, RcbotError):
            return self._source_crashed(error)
        if isinstance(error, SessionExpired):
            return await self._reauth(str(error))
        if isinstance(error, ManualStepRequired):
            return self._needs_manual(str(error))
        if isinstance(error, RateLimited):
            return await self._rate_limited(str(error))
        if isinstance(error, UnexpectedResponse):
            return self._unexpected(str(error))
        if isinstance(error, SourceNotConfigured):
            if self._switch_to_fallback(str(error)):
                return self._delay(MIN_POLL_INTERVAL_SEC)
            return self._backoff(str(error))
        if isinstance(error, (SiteUnavailable, HostNotAllowed)):
            return self._backoff(str(error))
        return self._backoff(f"{type(error).__name__}: {error}")

    def _source_crashed(self, error: BaseException) -> float:
        """Источник бросил не наше исключение (баг адаптера, сбой браузера): ведём себя как при
        недоступности сайта. Текст исключения не пишем: в нём бывают адреса с номером заявки."""
        reason = f"неожиданный сбой источника: {type(error).__name__}"
        logger.error("%s (%s)", reason, _where(error))
        self._journal.event("check.crash", reason, "ERROR")
        self._spawn(self.reset_source())
        return self._backoff(reason)

    def _loop_crashed(self, error: BaseException) -> float:
        reason = f"сбой цикла опроса: {type(error).__name__}"
        logger.error("%s (%s)", reason, _where(error))
        self._journal.event("poll.crash", reason, "ERROR")
        return self._backoff(reason)

    def _record_check(
        self, snapshot: AvailabilitySnapshot | None, latency_ms: float, error: BaseException | None
    ) -> None:
        self._journal.check(
            CheckRecord(
                at=self._clock.now(),
                source=self._source.kind,
                ok=error is None,
                slots=snapshot.slots if snapshot else frozenset(),
                latency_ms=latency_ms,
                state=self.status.state.value,
                interval_sec=self.status.interval_sec,
                error_kind=type(error).__name__ if error else "",
                error=_describe(error) if error else "",
            )
        )

    async def reset_source(self) -> None:
        try:
            await self._source.close()
        except Exception as error:  # закрытие сломанного браузера может падать по-разному
            logger.warning("источник не закрылся чисто: %s", type(error).__name__)

    @property
    def next_due(self) -> float:
        return self._next_due

    def snapshot(self) -> dict[str, object]:
        """Состояние для status.json журнала наблюдения."""
        s = self.status
        now = self._clock.now()
        mono = self._clock.monotonic()
        paused = self.runtime.paused_until if self.runtime.paused(now) else None
        in_progress = None if self.tick_started_at is None else mono - self.tick_started_at
        return {
            "state": s.state.value,
            "state_reason": s.state_reason,
            "source": self._source.kind.value,
            "interval_sec": round(self.current_interval(now), 1),
            "base_interval_sec": self.runtime.base_interval_sec,
            "months_ahead": self.runtime.months_ahead,
            "paused_until": paused.isoformat() if paused else None,
            "started_at": s.started_at.isoformat(),
            "last_check_at": s.last_check_at.isoformat() if s.last_check_at else None,
            "last_latency_ms": s.last_latency_ms,
            "next_check_in_sec": round(max(0.0, self._next_due - mono), 1),
            "check_in_progress_sec": None if in_progress is None else round(in_progress, 1),
            "checks_total": s.checks_total,
            "errors_total": s.errors_total,
            "last_error": s.last_error,
            "last_error_at": s.last_error_at.isoformat() if s.last_error_at else None,
        }

    def _schedule_next(self) -> float:
        now_mono = self._clock.monotonic()
        interval = self.current_interval(self._clock.now())
        self.status.interval_sec = interval
        factor = 1 + self._rng.uniform(-self._jitter, self._jitter)
        # Разброс не опускает интервал ниже минимума источника: 10 с у API, 60 с у DomSource
        self._next_due += max(self._min_spacing(), interval * factor)
        if self._next_due < now_mono:
            # Проверка длилась дольше интервала: без «догоняющей» серии запросов
            self._next_due = now_mono + MIN_POLL_INTERVAL_SEC
        return self._next_due - now_mono

    def _delay(self, seconds: float) -> float:
        self._next_due = self._clock.monotonic() + seconds
        return seconds

    # --- ошибки ---

    async def _reauth(self, reason: str) -> float:
        if self.status.state is not PollState.BACKOFF:
            # Из BACKOFF в REAUTH и обратно не прыгаем: каждый возврат слал бы владельцу
            # сообщение «Бот замедлился» заново
            self._set_state(PollState.REAUTH, reason)
        self._session.mark_expired()
        self._journal.event("login.start", f"вход на сайт: {reason}")
        try:
            await self._session.relogin(reason)
        except ReloginDeferred:
            return self._delay(max(REAUTH_RETRY_SEC, self._session.quiet_remaining()))
        except ReloginBudgetExceeded as error:
            self._set_state(PollState.BACKOFF, str(error))
            return self._delay(BACKOFF_MAX_SEC)
        except ManualStepRequired as error:
            return self._needs_manual(str(error))
        except RateLimited as error:
            # Страница входа заблокирована шлюзом: не стучимся каждые 30 с, а отступаем (D4)
            return await self._rate_limited(f"вход: {error}")
        except Exception as error:  # вход мог упасть и не нашим исключением
            description = _describe(error)
            self._reauth_failures += 1
            self._journal.event("login.failed", f"вход не удался: {description}", "WARNING")
            self._error(f"вход не удался: {description}")
            if self._reauth_failures >= REAUTH_FAILURES_TO_BACKOFF:
                self._set_state(
                    PollState.BACKOFF, f"{self._reauth_failures} неудачных входов подряд"
                )
                self._backoff_step += 1
                return self._delay(self._backoff_delay())
            return self._delay(REAUTH_RETRY_SEC)
        self._reauth_failures = 0
        self._journal.event("login.ok", "вход выполнен")
        self._set_state(PollState.RUNNING, "вход выполнен")
        return self._delay(0.0)

    def _needs_manual(self, reason: str) -> float:
        if self.status.state is not PollState.NEEDS_MANUAL:
            self._set_state(PollState.NEEDS_MANUAL, reason)
            self._bus.publish(ManualActionRequired(self._clock.now(), reason))
        elif self._manual_confirmed:
            # Владелец нажал «Готово», а сайт всё ещё просит шаг 1: напоминаем ему один раз
            self._bus.publish(ManualActionRequired(self._clock.now(), reason))
        self._manual_confirmed = False
        # Если владелец пройдёт шаг 1 и не нажмёт «Готово», бот сам заметит это через 15 минут
        return self._delay(MANUAL_RECHECK_SEC)

    async def _rate_limited(self, reason: str) -> float:
        self._error(reason)
        self.runtime.step_back(self._clock.now())
        self._set_state(PollState.BACKOFF, f"сайт ограничил запросы: {reason}; пауза 15 минут")
        delay = self._delay(RATE_LIMIT_PAUSE_SEC)
        try:
            await self._runtime_store.save(self.runtime)
        except Exception as error:  # пауза уже назначена: сбой записи не должен её отменить
            logger.warning("шаг назад по лестнице не сохранён: %s", type(error).__name__)
        return delay

    def _unexpected(self, reason: str) -> float:
        self._unexpected_streak += 1
        if (
            self._source.kind is SourceKind.API
            and self._unexpected_streak >= UNEXPECTED_TO_SWITCH
            and self._switch_to_fallback(f"{self._unexpected_streak} неожиданных ответа подряд")
        ):
            self._error(reason)  # и последний неожиданный ответ — тоже ошибка проверки
            return self._delay(MIN_POLL_INTERVAL_SEC)
        return self._backoff(reason)

    def _switch_to_fallback(self, reason: str) -> bool:
        if self._fallback is None or self._fallback is self._source:
            return False
        old = self._source
        self._source, self._fallback = self._fallback, None
        self._unexpected_streak = 0
        # Новый источник ещё не опрашивался, но запросы к сайту не чаще минимума
        self._not_before = self._clock.monotonic() + MIN_POLL_INTERVAL_SEC
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
            self._event_log.write(AppEvent(self._clock.now(), level, f"state.{state}", reason))
        )
        logger.info("состояние опроса %s → %s: %s", old, state, reason)

    def _error(self, reason: str) -> None:
        self.status.errors_total += 1
        self.status.last_error = reason
        self.status.last_error_at = self._clock.now()
        self._count(error=True)
        logger.warning("проверка не удалась: %s", reason)

    def _count_relogins(self) -> None:
        """Вход мог пройти внутри проверки или по команде: считаем разницу, а не события."""
        done = self._session.logins - self._logins_counted
        if done > 0:
            self._logins_counted = self._session.logins
            self._count(relogins=done)

    def _count(
        self, *, latency_ms: float | None = None, error: bool = False, relogins: int = 0
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
        self._hour.relogins += relogins

    async def _flush_hour(self) -> None:
        if self._hour is not None:
            try:
                await self._stats.save_hour(self._hour)
            except Exception as error:  # остановку не задерживаем из-за сбоя записи статистики
                logger.warning("статистика часа не сохранена: %s", type(error).__name__)
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background_done)

    def _background_done(self, task: asyncio.Task[None]) -> None:
        self._background.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("фоновая задача цикла упала: %s", type(error).__name__)

    def current_hour(self) -> PollStatsHour | None:
        return self._hour

    @property
    def source_kind(self) -> SourceKind:
        return self._source.kind
