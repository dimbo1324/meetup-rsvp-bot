"""Помощники тестов рассылки, учёта инцидентов и управления (зона T3).

`make_rig` собирает рассылку на фейках и ручных часах; `make_control` добавляет настоящий
`PollLoop` на фейковом источнике — он нужен `Controller`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.fakes import (
    AGENDA_URL,
    T0,
    TARGET,
    FakeAuthenticator,
    FakeNotifier,
    FakeSource,
    ManualClock,
    MemoryEventLog,
    MemoryIncidentRepository,
    MemoryKV,
    MemoryStatsRepository,
    RecordingJournal,
    slots,
    snapshot,
)

from rcbot.application.alerting import AlertOrchestrator, RepeatPolicy
from rcbot.application.bus import EventBus
from rcbot.application.control import Controller
from rcbot.application.incidents import IncidentBook
from rcbot.application.messages import MessageComposer
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import Coordinator, NoopCoordinator
from rcbot.application.runtime import RuntimeSettings, RuntimeStore
from rcbot.application.session_manager import SessionManager
from rcbot.domain.detector import Detector
from rcbot.domain.events import SlotsAppeared, SlotsChanged, SlotsGone
from rcbot.domain.models import Alert, Channel, DeliveryResult, Slot

MONTEVIDEO = ZoneInfo("America/Montevideo")

# Расписание повторов из SPEC 2.4 и раздела 5
SPEC_POLICIES: dict[Channel, RepeatPolicy] = {
    Channel.TELEGRAM: RepeatPolicy(60.0, 30),
    Channel.NTFY: RepeatPolicy(120.0, 10),
    Channel.CALL: RepeatPolicy(180.0, 5),
}


class TimedNotifier(FakeNotifier):
    """Канал, который запоминает, через сколько секунд после старта часов ушла каждая отправка."""

    def __init__(self, channel: Channel, clock: ManualClock, **kwargs: object) -> None:
        super().__init__(channel, **kwargs)  # type: ignore[arg-type]
        self._clock = clock
        self._start = clock.now()
        self.times: list[float] = []

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        result = await super().send(alert, attempt)
        self.times.append((self._clock.now() - self._start).total_seconds())
        return result


@dataclass
class Rig:
    clock: ManualClock
    notifiers: dict[Channel, TimedNotifier]
    repo: MemoryIncidentRepository
    book: IncidentBook
    session: SessionManager
    runtime: RuntimeSettings
    journal: RecordingJournal
    alerts: AlertOrchestrator
    composer: MessageComposer
    delivered: list[DeliveryResult] = field(default_factory=list)

    def sent(self, channel: Channel) -> list[tuple[Alert, int]]:
        return self.notifiers[channel].sent

    def attempts(self, channel: Channel) -> list[int]:
        return self.notifiers[channel].attempts


def make_rig(
    *,
    policies: dict[Channel, RepeatPolicy] | None = None,
    coordinator: Coordinator | None = None,
    channels: set[Channel] | None = None,
    notifier_kwargs: dict[Channel, dict[str, object]] | None = None,
    clock: ManualClock | None = None,
) -> Rig:
    clock = clock or ManualClock()
    kwargs = notifier_kwargs or {}
    notifiers = {
        channel: TimedNotifier(channel, clock, **kwargs.get(channel, {}))
        for channel in (channels or set(Channel))
    }
    repo = MemoryIncidentRepository()
    book = IncidentBook(repo, clock, TARGET.id)
    session = SessionManager(FakeAuthenticator(), clock, max_per_hour=6)
    runtime = RuntimeSettings(base_interval_sec=20.0, months_ahead=6, ladder_started_at=T0)
    journal = RecordingJournal()
    composer = MessageComposer(MONTEVIDEO, AGENDA_URL, "TEST-1")
    alerts = AlertOrchestrator(
        dict(notifiers),  # type: ignore[arg-type]
        SPEC_POLICIES if policies is None else policies,
        composer,
        book,
        session,
        runtime,
        clock,
        coordinator or NoopCoordinator(),
        journal=journal,
    )
    rig = Rig(clock, notifiers, repo, book, session, runtime, journal, alerts, composer)
    alerts.delivery_listeners.append(rig.delivered.append)
    return rig


def appeared(*days: int, at: datetime = T0) -> SlotsAppeared:
    return SlotsAppeared(at, snapshot(slots(*days), at=at))


def changed(*days: int, added: frozenset[Slot] | None = None, at: datetime = T0) -> SlotsChanged:
    found = slots(*days)
    return SlotsChanged(at, snapshot(found, at=at), added if added is not None else found)


def gone(at: datetime = T0) -> SlotsGone:
    return SlotsGone(at, snapshot(frozenset(), at=at))


def hang(clock: ManualClock, seconds: float) -> Callable[[], Awaitable[None]]:
    """Отправка, которая «занимает» заданное число секунд ручных часов."""

    async def delay() -> None:
        await clock.sleep(seconds)

    return delay


@dataclass
class ControlRig:
    rig: Rig
    loop: PollLoop
    controller: Controller
    source: FakeSource
    kv: MemoryKV
    event_log: MemoryEventLog
    bus: EventBus
    cleanups: list[int] = field(default_factory=list)
    liveness_text: str = ""


def make_control(
    *,
    cleanup_result: dict[str, int] | None = None,
    folder_bytes: int | None = 5 * 2**20,
    journal_bytes: int | None = 3 * 2**20,
    with_hooks: bool = True,
) -> ControlRig:
    rig = make_rig()
    source = FakeSource([frozenset()], clock=rig.clock)
    kv = MemoryKV()
    event_log = MemoryEventLog()
    bus = EventBus()
    loop = PollLoop(
        primary=source,
        fallback=None,
        session=rig.session,
        detector=Detector(),
        bus=bus,
        clock=rig.clock,
        runtime=rig.runtime,
        runtime_store=RuntimeStore(kv),
        stats=MemoryStatsRepository(),
        event_log=event_log,
        target=TARGET,
        jitter=0.1,
        journal=rig.journal,
    )
    state = ControlRig(rig, loop, None, source, kv, event_log, bus)  # type: ignore[arg-type]

    async def cleanup() -> dict[str, int]:
        state.cleanups.append(1)
        return cleanup_result or {}

    state.controller = Controller(
        loop,
        rig.alerts,
        rig.book,
        rig.repo,
        event_log,
        rig.session,
        rig.clock,
        cleanup=cleanup if with_hooks else None,
        folder_size=(lambda: folder_bytes or 0)
        if with_hooks and folder_bytes is not None
        else None,
        journal=rig.journal,
        journal_size=(lambda: journal_bytes or 0)
        if with_hooks and journal_bytes is not None
        else None,
        liveness=(lambda: state.liveness_text) if with_hooks else None,
    )
    return state
