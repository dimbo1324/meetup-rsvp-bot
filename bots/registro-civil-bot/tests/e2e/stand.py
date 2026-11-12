"""Стенд: весь граф `runner.run_app` без Telegram-сети и без Chromium.

Настоящее: `PollLoop`, `EventBus`, `AlertOrchestrator`, `IncidentBook`, `JournalFeed`,
`SessionManager`, `Controller`, `LivenessMonitor`, `Housekeeping`, `Supervisor`, файловая SQLite с
миграциями, `FileJournal` в временной папке, `ApiSource` поверх `SafeHttp` и локального TLS-сервера.
Подставное: вход (`SiteAuthenticator`), канал Telegram, запасной источник (вместо `DomSource`).
Каналы ntfy и CallMeBot — фальшивые или настоящие против локального сервера (`channels=`).

Порядок остановки и подписки событий — те же функции, что в `runner` (`_wire`, `_shutdown`,
`_policies`): стенд не разойдётся с корнем сборки молча.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from rcbot.application.alerting import AlertOrchestrator
from rcbot.application.bus import EventBus
from rcbot.application.control import Controller
from rcbot.application.housekeeping import Housekeeping
from rcbot.application.incidents import IncidentBook
from rcbot.application.journal_feed import JournalFeed
from rcbot.application.liveness import LivenessMonitor
from rcbot.application.messages import MessageComposer
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import (
    Authenticator,
    AvailabilitySource,
    CheckRecord,
    NoopCoordinator,
    Notifier,
    OpeningRecord,
)
from rcbot.application.runtime import K_LADDER_START, RuntimeSettings, RuntimeStore
from rcbot.application.session_manager import SessionManager
from rcbot.application.supervisor import Supervisor
from rcbot.bootstrap import Core, Storage, ensure_target, open_storage
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretStore
from rcbot.config.settings import Settings
from rcbot.domain.detector import Detector
from rcbot.domain.models import Alert, Channel, DeliveryResult, SourceKind, WatchTarget
from rcbot.infrastructure.files import FileJanitor
from rcbot.infrastructure.gubuy import api_source as api_source_module
from rcbot.infrastructure.gubuy.api_source import ApiSource
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.journal.files import FileJournal
from rcbot.infrastructure.logging.redaction import REDACTOR
from rcbot.infrastructure.notify import callmebot as callmebot_module
from rcbot.infrastructure.notify.callmebot import CallMeBotNotifier
from rcbot.infrastructure.notify.ntfy import NtfyNotifier
from rcbot.runner import Closers, _policies, _shutdown, _wire
from tests.e2e.clock import StandClock
from tests.e2e.site import ChannelsSite, FakeSite, SiteAuthenticator, parse_test_payload
from tests.fakes import AGENDA_URL, FakeNotifier, FakeSource

TRAMITE_ID = "TEST-TRAMITE-0001"
NTFY_TOPIC = "test-topic-random-0123456789abcdefghijklmnop"
CALL_USER = "@test_owner"
STOP_MESSAGE_CODE = "bot.stop"


# --- журнал и каналы под наблюдением ---


class StandJournal(FileJournal):
    """Настоящий `FileJournal` с чтением записанного и искусственным «зависанием диска»."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.check_count = 0
        self.stall_sec = 0.0

    def _stall(self) -> None:
        if self.stall_sec:
            time.sleep(self.stall_sec)  # блокирует цикл событий, как зависший диск

    def check(self, record: CheckRecord) -> None:
        self._stall()
        self.check_count += 1
        super().check(record)

    def event(self, code: str, message: str, level: str = "INFO", **data: object) -> None:
        self._stall()
        super().event(code, message, level, **data)

    def opening(self, record: OpeningRecord) -> None:
        self._stall()
        super().opening(record)

    def _jsonl(self, folder: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted((self.root / folder).glob("*.jsonl")):
            rows += [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]
        return rows

    def read_checks(self) -> list[dict[str, Any]]:
        return self._jsonl("checks")

    def read_events(self) -> list[dict[str, Any]]:
        return self._jsonl("events")

    def codes(self) -> list[str]:
        return [e["code"] for e in self.read_events()]

    def read_openings(self) -> list[dict[str, str]]:
        path = self.root / "openings.csv"
        if not path.exists():
            return []
        with path.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))

    def read_status(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.root / "status.json").read_text("utf-8"))
        return data


@dataclass
class Sent:
    attempt: int
    alert: Alert
    mono: float
    perf_start: float
    perf_end: float
    result: DeliveryResult


class SpyNotifier:
    """Обёртка над любым каналом: запоминает вызовы `send`, моменты и ведёт счёт «в работе»."""

    def __init__(self, inner: Notifier, clock: StandClock) -> None:
        self.inner = inner
        self._clock = clock
        self.sent: list[Sent] = []
        self.in_flight = 0
        self.warmups = 0

    @property
    def channel(self) -> Channel:
        return self.inner.channel

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        self.in_flight += 1
        start = time.perf_counter()
        mono = self._clock.monotonic()
        try:
            result = await self.inner.send(alert, attempt)
        finally:
            self.in_flight -= 1
        self.sent.append(Sent(attempt, alert, mono, start, time.perf_counter(), result))
        return result

    async def warmup(self) -> None:
        self.warmups += 1
        await self.inner.warmup()

    async def close(self) -> None:
        await self.inner.close()

    @property
    def attempts(self) -> list[int]:
        return [s.attempt for s in self.sent]

    @property
    def texts(self) -> list[str]:
        return [s.alert.text for s in self.sent]


# --- стенд ---


@dataclass
class Stand:
    paths: Paths
    settings: Settings
    clock: StandClock
    journal: StandJournal
    storage: Storage
    supervisor: Supervisor
    closers: Closers
    bus: EventBus
    book: IncidentBook
    detector: Detector
    alerts: AlertOrchestrator
    loop: PollLoop
    controller: Controller
    liveness: LivenessMonitor
    housekeeping: Housekeeping
    session: SessionManager
    runtime: RuntimeSettings
    target: WatchTarget
    primary: AvailabilitySource
    fallback: AvailabilitySource | None
    notifiers: dict[Channel, SpyNotifier]
    auth: Authenticator
    t0: float
    ignore: set[str] = field(default_factory=set)
    closed: bool = False

    # --- сборка ---

    @classmethod
    async def build(
        cls,
        *,
        paths: Paths,
        journal_root: Path,
        clock: StandClock,
        monkeypatch: pytest.MonkeyPatch,
        site: FakeSite | None = None,
        primary: AvailabilitySource | None = None,
        fallback: AvailabilitySource | None = None,
        auth: Authenticator | None = None,
        channels: ChannelsSite | None = None,
        notifiers: dict[Channel, Any] | None = None,
        ladder_age_hours: float | None = None,
        jitter: float = 0.0,
        journal_max_bytes: int = 10 * 2**30,
        journal_trim_bytes: int = 5 * 2**30,
        incidents_open_hook: Callable[[], Awaitable[None]] | None = None,
        start: bool = True,
        **settings_overrides: Any,
    ) -> Stand:
        paths.ensure()
        settings = Settings(
            agenda_url=AGENDA_URL,
            tramite_id=TRAMITE_ID,
            poll_jitter=jitter,
            **settings_overrides,
        )
        # Как в build_core: номера заявки и этапа не должны попасть ни в журнал, ни в лог
        REDACTOR.register_many([TRAMITE_ID, "TEST-ETAPA"])
        journal = StandJournal(
            journal_root, settings.tz, clock, journal_max_bytes, journal_trim_bytes
        )
        core = Core(paths, settings, SecretStore(), clock=clock, journal=journal)  # type: ignore[arg-type]
        storage = await open_storage(core)
        if incidents_open_hook is not None:
            _slow_down_open(storage, incidents_open_hook)
        target = await ensure_target(core, storage)
        if ladder_age_hours is not None and await storage.kv.get(K_LADDER_START) is None:
            started = clock.now() - timedelta(hours=ladder_age_hours)
            await storage.kv.set(K_LADDER_START, started.isoformat())
        runtime_store = RuntimeStore(storage.kv)
        runtime = await runtime_store.load(
            settings.poll_interval_sec, settings.months_ahead, clock.now()
        )

        # Источники: ApiSource поверх настоящего SafeHttp; вместо DomSource — запасной по сценарию
        if primary is None:
            assert site is not None, "нужен либо site, либо primary"
            monkeypatch.setattr(api_source_module, "parse_disponibilidades", parse_test_payload)
            # Как в build_api_source: SafeHttp без своего jar. Куки для IP-адреса aiohttp по
            # умолчанию не хранит; включаем unsafe только для адреса 127.0.0.1 тестового сервера
            monkeypatch.setattr(
                aiohttp, "CookieJar", lambda *a, **kw: _UNSAFE_JAR(*a, unsafe=True, **kw)
            )
            http = SafeHttp(
                {"127.0.0.1"},
                timeout_sec=settings.request_timeout_sec,
                ssl_context=site.server.client_ssl,
            )
            primary = ApiSource(http, site.api_url, AGENDA_URL, paths.quarantine, clock)
        if auth is None:
            assert site is not None, "нужен либо site, либо auth"
            auth = SiteAuthenticator(site)
        session = SessionManager(auth, clock, settings.relogin_max_per_hour)
        sources = [src for src in (primary, fallback) if src is not None]
        for source in sources:
            session.subscribe(source.use_cookies)

        book = IncidentBook(storage.incidents, clock, target.id)
        detector = Detector()
        if (restored := await book.restore()) is not None:
            detector.restore(restored.slots)

        spies = _build_notifiers(notifiers, channels, clock, monkeypatch)
        bus = EventBus()
        composer = MessageComposer(settings.tz, settings.agenda_url, settings.tramite_id)
        alerts = AlertOrchestrator(
            dict(spies),  # type: ignore[arg-type]
            _policies(core),
            composer,
            book,
            session,
            runtime,
            clock,
            NoopCoordinator(),
            journal=journal,
        )
        loop = PollLoop(
            primary=primary,
            fallback=fallback,
            session=session,
            detector=detector,
            bus=bus,
            clock=clock,
            runtime=runtime,
            runtime_store=runtime_store,
            stats=storage.stats,
            event_log=storage.events,
            target=target,
            jitter=settings.poll_jitter,
            rng=random.Random(7),
            journal=journal,
        )
        _wire(bus, alerts, book)
        JournalFeed(journal, clock).subscribe(bus)
        housekeeping = Housekeeping(
            storage.maintenance, FileJanitor(paths), storage.kv, clock, journal
        )
        supervisor = Supervisor(clock)

        async def restart_poll() -> None:
            await loop.reset_source()
            await supervisor.restart("poll")

        async def liveness_extra() -> dict[str, object]:
            incident = await book.current()
            days = sorted({x.day.isoformat() for x in incident.slots}) if incident else []
            return {
                "incident_open": incident is not None,
                "incident_days": days,
                "alert_repeating": alerts.repeating(),
                "logins_last_hour": session.logins_last_hour(),
            }

        liveness = LivenessMonitor(
            loop, journal, clock, alerts.service, restart_poll, liveness_extra
        )
        controller = Controller(
            loop,
            alerts,
            book,
            storage.incidents,
            storage.events,
            session,
            clock,
            cleanup=housekeeping.run_once,
            folder_size=housekeeping.folder_size,
            journal=journal,
            journal_size=journal.size,
            liveness=liveness.diagnose,
        )
        closers = Closers()
        closers.add(alerts.close)
        closers.add(bus.drain)
        for source in sources:
            closers.add(source.close)
        for spy in spies.values():
            closers.add(spy.close)

        stand = cls(
            paths=paths,
            settings=settings,
            clock=clock,
            journal=journal,
            storage=storage,
            supervisor=supervisor,
            closers=closers,
            bus=bus,
            book=book,
            detector=detector,
            alerts=alerts,
            loop=loop,
            controller=controller,
            liveness=liveness,
            housekeeping=housekeeping,
            session=session,
            runtime=runtime,
            target=target,
            primary=primary,
            fallback=fallback,
            notifiers=spies,
            auth=auth,
            t0=clock.monotonic(),
        )
        if start:
            stand.start()
            await stand.quiesce()
        return stand

    def start(
        self,
        *,
        poll: bool = True,
        liveness: bool = True,
        housekeeping: bool = False,
        quota: bool = False,
        warmup: bool = False,
    ) -> None:
        self.housekeeping.start()
        if warmup:
            self.supervisor.start("warmup", self.alerts.keep_warm)
        if housekeeping:
            self.supervisor.start("housekeeping", self.housekeeping.run_forever)
        if quota:
            self.supervisor.start("journal-quota", self.housekeeping.journal_quota_forever)
        if poll:
            self.supervisor.start("poll", self.loop.run)
        if liveness:
            self.supervisor.start("liveness", self.liveness.run)

    # --- время и тишина ---

    @property
    def elapsed(self) -> float:
        """Секунды с момента сборки стенда по часам стенда."""
        return self.clock.monotonic() - self.t0

    def busy(self) -> str:
        """Что ещё не закончило работу; пустая строка — стенд «тих»."""
        reasons = []
        if "tick" not in self.ignore and self.loop.tick_started_at is not None:
            reasons.append("проверка в работе")
        if "send" not in self.ignore and any(s.in_flight for s in self.notifiers.values()):
            reasons.append("отправка в канал")
        if "bus" not in self.ignore and self.bus._tasks:
            reasons.append(f"подписчики шины: {len(self.bus._tasks)}")
        if "background" not in self.ignore and (self.alerts._background or self.loop._background):
            reasons.append("фоновая запись")
        return ", ".join(reasons)

    async def quiesce(self, limit: float = 10.0) -> None:
        """Крутить цикл событий, пока стенд не затихнет (сокеты и потоки SQLite заканчивают)."""
        deadline = time.perf_counter() + limit
        calm = 0
        while calm < 5:
            await asyncio.sleep(0)
            if self.busy():
                calm = 0
                await asyncio.sleep(0.001)
            else:
                calm += 1
            if time.perf_counter() > deadline:
                raise AssertionError(f"стенд не затих за {limit} с: {self.busy()}")

    async def advance(self, seconds: float) -> None:
        """Сдвинуть часы на `seconds`, будя спящих по одному и ожидая тишину после каждого."""
        target = self.clock.monotonic() + seconds
        await self.quiesce()
        while (deadline := self.clock.next_deadline()) is not None and deadline <= target:
            self.clock.fire_next()
            await self.quiesce()
        self.clock.jump_to(target)
        await self.quiesce()

    async def next_check(self) -> None:
        """Дойти до ближайшей запланированной проверки и дождаться её конца."""
        wait = max(0.0, self.loop.next_due - self.clock.monotonic())
        await self.advance(wait)

    async def checks(self, count: int) -> None:
        for _ in range(count):
            await self.next_check()

    async def until(self, condition: Callable[[], bool], limit: float = 10.0) -> None:
        """Ждать настоящего времени, пока условие не выполнится (для сокетов, без часов стенда)."""
        deadline = time.perf_counter() + limit
        while not condition():
            if time.perf_counter() > deadline:
                raise AssertionError("условие не выполнилось за отведённое время")
            await asyncio.sleep(0.001)

    # --- чтение состояния ---

    def sent(self, channel: Channel) -> list[Sent]:
        return self.notifiers[channel].sent

    def texts(self, channel: Channel) -> list[str]:
        return self.notifiers[channel].texts

    async def incidents(self) -> list[Any]:
        return await self.storage.incidents.last(50)

    # --- остановка ---

    async def stop(self) -> None:
        """Штатная остановка, как в `runner.run_app` (`finally`)."""
        if self.closed:
            return
        self.closed = True
        await self.quiesce_quietly()
        await _shutdown(self.loop, self.alerts, self.supervisor, self.closers)
        await self.storage.close()
        self.journal.close()

    async def crash(self) -> None:
        """Резкая остановка без сообщения об остановке: задачи отменены, ресурсы закрыты."""
        if self.closed:
            return
        self.closed = True
        await self.supervisor.stop()
        await self.closers.close_all()
        await self.storage.close()
        self.journal.close()

    async def quiesce_quietly(self) -> None:
        with contextlib.suppress(AssertionError):  # остановка нужна как раз зависшему стенду
            await self.quiesce(limit=2.0)


# --- вспомогательное ---

_UNSAFE_JAR = aiohttp.CookieJar


def _slow_down_open(storage: Storage, hook: Callable[[], Awaitable[None]]) -> None:
    repo = storage.incidents
    original = repo.open

    async def slow_open(*args: Any, **kwargs: Any) -> Any:
        await hook()
        return await original(*args, **kwargs)

    repo.open = slow_open  # type: ignore[method-assign]


def _build_notifiers(
    given: dict[Channel, Any] | None,
    channels: ChannelsSite | None,
    clock: StandClock,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[Channel, SpyNotifier]:
    inner: dict[Channel, Notifier] = {c: FakeNotifier(c) for c in Channel}
    if channels is not None:
        ssl_context = channels.server.client_ssl
        monkeypatch.setattr(callmebot_module, "API_URL", channels.callmebot_url)
        inner[Channel.NTFY] = NtfyNotifier(
            SafeHttp({"127.0.0.1"}, 5, ssl_context=ssl_context), channels.ntfy_server, NTFY_TOPIC
        )
        inner[Channel.CALL] = CallMeBotNotifier(
            SafeHttp({"127.0.0.1"}, 5, ssl_context=ssl_context), CALL_USER, "ru-RU-Standard-A"
        )
    if given:
        inner.update(given)
    return {channel: SpyNotifier(notifier, clock) for channel, notifier in inner.items()}


def fake_source(
    clock: StandClock, *script: Any, kind: SourceKind = SourceKind.DOM, **kwargs: Any
) -> FakeSource:
    return FakeSource(list(script), kind=kind, clock=clock, **kwargs)  # type: ignore[arg-type]
