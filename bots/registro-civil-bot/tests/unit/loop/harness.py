"""Стенд для тестов цикла опроса: настоящий PollLoop на фальшивых часах и адаптерах.

Цикл работает на реальном цикле событий, но время идёт только по `await clock.advance(n)`
(`ManualClock`): моменты проверок видны с точностью до секунды и не зависят от скорости ПК.
Помощник живёт в каталоге зоны (T2), общие фейки — в `tests/fakes.py`.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tests.fakes import (
    T0,
    TARGET,
    FakeAuthenticator,
    FakeSource,
    ManualClock,
    MemoryEventLog,
    MemoryKV,
    MemoryStatsRepository,
    RecordingJournal,
    ScriptItem,
    settle,
)

from rcbot.application.bus import EventBus
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import AvailabilitySource, SessionCookie
from rcbot.application.runtime import RuntimeSettings, RuntimeStore
from rcbot.application.session_manager import SessionManager
from rcbot.domain.detector import Detector
from rcbot.domain.events import DomainEvent
from rcbot.domain.models import AvailabilitySnapshot, SourceKind, WatchTarget


class TimedSource(FakeSource):
    """FakeSource, который запоминает монотонный момент начала каждой проверки."""

    def __init__(
        self,
        script: Sequence[ScriptItem] = (),
        *,
        clock: ManualClock,
        kind: SourceKind = SourceKind.API,
        **kwargs: object,
    ) -> None:
        super().__init__(script, kind=kind, clock=clock, **kwargs)  # type: ignore[arg-type]
        self._timed_clock = clock
        self.starts: list[float] = []
        self.targets: list[WatchTarget] = []

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        self.starts.append(self._timed_clock.monotonic())
        self.targets.append(target)
        return await super().fetch(target)


@dataclass
class Rig:
    clock: ManualClock
    loop: PollLoop
    source: TimedSource
    session: SessionManager
    auth: FakeAuthenticator
    bus: EventBus
    runtime: RuntimeSettings
    kv: MemoryKV
    stats: MemoryStatsRepository
    event_log: MemoryEventLog
    journal: RecordingJournal
    events: list[DomainEvent] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    fallback: TimedSource | None = None
    t_start: float = 1000.0

    async def start(self) -> None:
        self.task = asyncio.create_task(self.loop.run())
        await settle()

    async def stop(self) -> None:
        """Остановка по-хорошему: stop() и ожидание завершения run()."""
        self.loop.stop()
        await settle()
        if self.task is not None:
            await asyncio.wait_for(self.task, 1)

    async def kill(self) -> None:
        """Аварийная остановка отменой задачи (если тест не дошёл до stop())."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def advance(self, seconds: float) -> None:
        await self.clock.advance(seconds)

    def of_type[E: DomainEvent](self, kind: type[E]) -> list[E]:
        return [e for e in self.events if isinstance(e, kind)]

    @property
    def now_mono(self) -> float:
        return self.clock.monotonic()

    def rel(self, moments: Sequence[float]) -> list[float]:
        """Моменты относительно первой проверки — читаемые числа вместо 1000.0, 1060.0."""
        base = self.t_start
        return [round(m - base, 6) for m in moments]


def make_rig(
    script: Sequence[ScriptItem] = (),
    *,
    kind: SourceKind = SourceKind.API,
    fallback_script: Sequence[ScriptItem] | None = None,
    fallback_kind: SourceKind = SourceKind.DOM,
    jitter: float = 0.0,
    seed: int | None = None,
    rng: random.Random | None = None,
    base_interval: float = 20.0,
    ladder_elapsed: timedelta = timedelta(hours=18),
    max_logins_per_hour: int = 6,
    quiet_after_alert_sec: float = 600.0,
    failures: Sequence[BaseException] = (),
    auth_delay: Callable[[], Awaitable[None]] | None = None,
    paused_until: datetime | None = None,
    months: int = 6,
    start: datetime = T0,
) -> Rig:
    """Собрать цикл. По умолчанию лестница D4 уже на базовом интервале (прошло 18 часов)."""
    clock = ManualClock(start)
    source = TimedSource(script, clock=clock, kind=kind)
    fallback = (
        TimedSource(fallback_script, clock=clock, kind=fallback_kind)
        if fallback_script is not None
        else None
    )
    auth = FakeAuthenticator(failures=failures, delay=auth_delay)
    session = SessionManager(auth, clock, max_logins_per_hour, quiet_after_alert_sec)
    session.subscribe(source.use_cookies)
    if fallback is not None:
        session.subscribe(fallback.use_cookies)
    bus = EventBus()
    kv = MemoryKV()
    runtime = RuntimeSettings(
        base_interval_sec=base_interval,
        months_ahead=months,
        ladder_started_at=start - ladder_elapsed,
        paused_until=paused_until,
    )
    stats = MemoryStatsRepository()
    event_log = MemoryEventLog()
    journal = RecordingJournal()
    loop = PollLoop(
        primary=source,
        fallback=fallback,
        session=session,
        detector=Detector(),
        bus=bus,
        clock=clock,
        runtime=runtime,
        runtime_store=RuntimeStore(kv),
        stats=stats,
        event_log=event_log,
        target=TARGET,
        jitter=jitter,
        rng=rng or random.Random(0 if seed is None else seed),
        journal=journal,
    )
    rig = Rig(
        clock, loop, source, session, auth, bus, runtime, kv, stats, event_log, journal,
        fallback=fallback,
    )  # fmt: skip
    rig.t_start = clock.monotonic()

    async def collect(event: DomainEvent) -> None:
        rig.events.append(event)

    bus.subscribe(DomainEvent, collect)
    return rig


def spans(moments: Sequence[float]) -> list[float]:
    """Промежутки между соседними моментами."""
    return [b - a for a, b in itertools.pairwise(moments)]


def cookie(name: str = "simple_bpm_session") -> SessionCookie:
    return SessionCookie(name, "TEST", "bpmgob.mec.gub.uy")


def as_source(source: TimedSource) -> AvailabilitySource:
    return source
