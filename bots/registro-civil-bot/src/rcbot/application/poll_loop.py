"""Цикл опроса (SPEC 2.2–2.3): расписание без дрейфа, состояния, отступление, смена источника.

Следующая проверка считается от `loop.time()` предыдущего срока с разбросом ±jitter, а не
«сон после окончания». Ошибки сайта замедляют опрос, а не учащают его (SPEC 0.3, правило 4).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import replace
from datetime import datetime
from pathlib import PurePath
from typing import Any

from rcbot.application.background import BackgroundTasks
from rcbot.application.bus import EventBus
from rcbot.application.hourly_stats import HourlyStats
from rcbot.application.ports import (
    AvailabilitySource,
    CheckRecord,
    Clock,
    EventLog,
    Journal,
    NullJournal,
    StatsRepository,
)
from rcbot.application.runtime import (
    RuntimeSettings,
    RuntimeStore,
    clamp_interval,
    clamp_months,
)
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
# Бессрочная пауза: состояние перепроверяется раз в это время (команда /resume будит цикл сразу)
PAUSE_RECHECK_SEC = 900.0
REAUTH_RETRY_SEC = 30.0
REAUTH_FAILURES_TO_BACKOFF = 3
UNEXPECTED_TO_SWITCH = 3
# Со скольки подряд идущих сбоев состояние становится BACKOFF и владельцу уходит сообщение
BACKOFF_STATE_AFTER = 2
# Проверка дольше этого прерывается, браузер перезапускается (защита от зависания)
FETCH_TIMEOUT_SEC = 150.0
# Вход внутри проверки (после SessionExpired) дольше этого прерывается; шаги входа в auth.py
# (30 с на шаг, 60 с на возврат, 45 с на навигацию) укладываются с запасом
LOGIN_TIMEOUT_SEC = 240.0


_ErrorHandler = Callable[[str], float | Awaitable[float]]


def describe_error(error: BaseException) -> str:
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
        self._event_log = event_log
        self._journal: Journal = journal or NullJournal()
        self._target = target
        self._jitter = jitter
        self._rng = rng or random.Random()
        self.status = PollStatus(started_at=clock.now(), source=primary.kind)
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._background = BackgroundTasks("цикл опроса")
        self._hourly = HourlyStats(clock, stats, self._background)
        self._next_due = 0.0
        # Не начинать проверку раньше этого момента: минимум между запросами (SPEC 0.3, правило 3)
        # держится при любых пробуждениях (/interval, «Готово», /resume, вход по команде)
        self._not_before = 0.0
        self._logins_counted = session.logins
        self._manual_confirmed = False
        self._backoff_step = 0
        self._reauth_failures = 0
        self._unexpected_streak = 0
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
        self.runtime.months_ahead = clamp_months(months)
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
                    pause_sec = (until - now).total_seconds() if until else PAUSE_RECHECK_SEC
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
            # Длительность — до разбора ошибки: повторный вход в «время ответа» не входит
            duration_ms = (self._clock.monotonic() - started) * 1000
            timed_out = isinstance(error, TimeoutError) and scope.expired()
            delay = await self._handle_error(error, timed_out=timed_out)
            self._record_check(None, duration_ms, error, interval_sec=delay)
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
        self._hourly.count(latency_ms=snapshot.latency_ms)
        event = self._detector.compare(snapshot)
        if event is not None:
            self._bus.publish(event)
        delay = self._schedule_next()
        self._record_check(
            snapshot, snapshot.latency_ms, None, interval_sec=self.status.interval_sec
        )
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
        for error_type, handler in self._error_handlers():
            if isinstance(error, error_type):
                outcome = handler(str(error))
                return await outcome if inspect.isawaitable(outcome) else outcome
        return self._backoff(f"{type(error).__name__}: {error}")

    def _error_handlers(self) -> tuple[tuple[type[RcbotError], _ErrorHandler], ...]:
        """Что делать с каждой ошибкой источника; прочие ошибки бота — обычное отступление."""
        return (
            (SessionExpired, self._reauth),
            (ManualStepRequired, self._needs_manual),
            (RateLimited, self._rate_limited),
            (UnexpectedResponse, self._unexpected),
            (SourceNotConfigured, self._source_not_configured),
            (SiteUnavailable, self._backoff),
            (HostNotAllowed, self._backoff),
        )

    def _source_not_configured(self, reason: str) -> float:
        if self._switch_to_fallback(reason):
            return self._delay(self._min_spacing())
        return self._backoff(reason)

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
        self,
        snapshot: AvailabilitySnapshot | None,
        latency_ms: float,
        error: BaseException | None,
        *,
        interval_sec: float,
    ) -> None:
        self._journal.check(
            CheckRecord(
                at=self._clock.now(),
                source=self._source.kind,
                ok=error is None,
                slots=snapshot.slots if snapshot else frozenset(),
                latency_ms=latency_ms,
                state=self.status.state.value,
                interval_sec=interval_sec,
                error_kind=type(error).__name__ if error else "",
                error=describe_error(error) if error else "",
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
            async with asyncio.timeout(LOGIN_TIMEOUT_SEC):
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
        except SiteUnavailable as error:
            # Сайт входа недоступен: то же экспоненциальное отступление, что и у проверки
            return self._backoff(f"вход: {error}")
        except Exception as error:  # вход мог упасть и не нашим исключением
            description = (
                f"вход не уложился в {LOGIN_TIMEOUT_SEC:g} с"
                if isinstance(error, TimeoutError)
                else describe_error(error)
            )
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
            return self._delay(self._min_spacing())
        return self._backoff(reason)

    def _switch_to_fallback(self, reason: str) -> bool:
        if self._fallback is None or self._fallback is self._source:
            return False
        old = self._source
        self._source, self._fallback = self._fallback, None
        self._unexpected_streak = 0
        # Новый источник ещё не опрашивался, но запросы к сайту не чаще минимума
        self._not_before = self._clock.monotonic() + self._min_spacing()
        self.status.source = self._source.kind
        self._bus.publish(SourceSwitched(self._clock.now(), old.kind, self._source.kind, reason))
        self._spawn(old.close())
        logger.warning("источник %s → %s: %s", old.kind, self._source.kind, reason)
        return True

    def _backoff(self, reason: str) -> float:
        self._error(reason)
        self._backoff_step += 1
        # Одиночный сбой сети — не повод беспокоить владельца: в BACKOFF со второго подряд
        if self._backoff_step >= BACKOFF_STATE_AFTER:
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
        self._hourly.count(error=True)
        logger.warning("проверка не удалась: %s", reason)

    def _count_relogins(self) -> None:
        """Вход мог пройти внутри проверки или по команде: считаем разницу, а не события."""
        done = self._session.logins - self._logins_counted
        if done > 0:
            self._logins_counted = self._session.logins
            self._hourly.count(relogins=done)

    async def _flush_hour(self) -> None:
        await self._hourly.flush()
        await self._background.drain()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        self._background.spawn(coro)

    def current_hour(self) -> PollStatsHour | None:
        return self._hourly.current

    @property
    def source_kind(self) -> SourceKind:
        return self._source.kind
