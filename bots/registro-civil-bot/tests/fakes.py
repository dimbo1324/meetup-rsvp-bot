"""Общие фейки для тестов rcbot (решение R7). Без сети, без реального времени, без секретов.

Что здесь есть и когда брать:
- `FakeClock` — время бежит само: `sleep` мгновенно сдвигает часы. Для линейных сценариев.
- `ManualClock` — время идёт только по `await clock.advance(sec)`; спящие задачи просыпаются в
  порядке сроков. Для повторов, пауз, расписаний, зависаний: проверяет точные моменты.
- `FakeSource`, `FakeAuthenticator`, `FakeNotifier` — адаптеры по сценарию с журналом вызовов.
- `Memory*` — реализации портов хранения в памяти (прикладным тестам БД не нужна).
- `RecordingJournal` — журнал наблюдения, который запоминает всё записанное.
- `LocalTlsServer` — настоящий HTTPS-сервер на 127.0.0.1 с сертификатом `trustme`: через него
  гоняется `SafeHttp` с реальными сокетами, TLS и таймаутами. Живой сайт в тестах не нужен и
  запрещён (SPEC 6.4).

Данные — синтетические: номера заявки и этапа в тестах условные (`TEST-…`), настоящих нет.
"""

from __future__ import annotations

import asyncio
import heapq
import ssl
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import trustme
from aiohttp import web

from rcbot.application.ports import (
    CheckRecord,
    OpeningRecord,
    SessionCookie,
)
from rcbot.domain.models import (
    Alert,
    AppEvent,
    AvailabilitySnapshot,
    Channel,
    DeliveryResult,
    Incident,
    IncidentStatus,
    PollStatsHour,
    Slot,
    SourceKind,
    WatchTarget,
)

T0 = datetime(2026, 11, 12, 15, 0, 0, tzinfo=UTC)  # 12:00 в Монтевидео
AGENDA_URL = "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1"
TARGET = WatchTarget(1, "registro-civil", AGENDA_URL, "Oficina Tramite Extranjeros", 6)


# --- данные ---


def slots(*days: int, year: int = 2027, month: int = 2) -> frozenset[Slot]:
    """Свободные дни месяца: `slots(23, 24)` → 23 и 24 февраля 2027."""
    return frozenset(Slot(date(year, month, day)) for day in days)


def slots_at(*moments: tuple[int, int, int], year: int = 2027, month: int = 2) -> frozenset[Slot]:
    """Слоты со временем: `slots_at((23, 10, 30), (23, 10, 45))`."""
    return frozenset(Slot(date(year, month, d), time(h, m)) for d, h, m in moments)


def snapshot(
    found: frozenset[Slot] = frozenset(),
    *,
    at: datetime = T0,
    source: SourceKind = SourceKind.DOM,
    latency_ms: float = 5.0,
    target_id: int = TARGET.id,
) -> AvailabilitySnapshot:
    return AvailabilitySnapshot(target_id, at, found, source, latency_ms)


# --- часы ---


async def settle(rounds: int = 12) -> None:
    """Дать готовым задачам цикла событий отработать (без реального ожидания)."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeClock:
    """Время бежит само: `sleep(n)` мгновенно сдвигает часы на n секунд."""

    def __init__(self, start: datetime = T0, mono: float = 1000.0) -> None:
        self._now = start
        self._mono = mono
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._now += timedelta(seconds=seconds)


class ManualClock:
    """Время идёт только по `await advance(sec)`; спящие задачи просыпаются по порядку сроков."""

    def __init__(self, start: datetime = T0, mono: float = 1000.0) -> None:
        self._now = start
        self._mono = mono
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    @property
    def sleeping(self) -> int:
        return sum(1 for _, _, fut in self._sleepers if not fut.done())

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._sleepers, (self._mono + seconds, self._seq, future))
        await future

    async def advance(self, seconds: float) -> None:
        target = self._mono + seconds
        while True:
            await settle()
            while self._sleepers and self._sleepers[0][2].done():
                heapq.heappop(self._sleepers)
            if not self._sleepers or self._sleepers[0][0] > target:
                break
            deadline, _, future = heapq.heappop(self._sleepers)
            self._set_mono(deadline)
            future.set_result(None)
        self._set_mono(target)
        await settle()

    def _set_mono(self, mono: float) -> None:
        self._now += timedelta(seconds=mono - self._mono)
        self._mono = mono


# --- источники, вход, каналы ---

ScriptItem = frozenset[Slot] | BaseException | Callable[[], Awaitable[frozenset[Slot]]]


class FakeSource:
    """Источник календаря по сценарию: набор слотов, исключение или корутина (например, зависание).

    После исчерпания сценария повторяет последний элемент (`repeat_last=True`) или падает.
    """

    def __init__(
        self,
        script: Sequence[ScriptItem] = (),
        *,
        kind: SourceKind = SourceKind.DOM,
        clock: FakeClock | ManualClock | None = None,
        latency_ms: float = 5.0,
        repeat_last: bool = True,
    ) -> None:
        self._script = list(script)
        self.kind = kind
        self._clock = clock
        self._latency_ms = latency_ms
        self._repeat_last = repeat_last
        self._last: ScriptItem | None = None
        self.calls: list[WatchTarget] = []
        self.cookie_history: list[list[SessionCookie]] = []
        self.closed = 0

    def push(self, *items: ScriptItem) -> None:
        self._script.extend(items)

    @property
    def remaining(self) -> int:
        return len(self._script)

    def use_cookies(self, cookies: Sequence[SessionCookie]) -> None:
        self.cookie_history.append(list(cookies))

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        self.calls.append(target)
        if self._script:
            item = self._script.pop(0)
            self._last = item
        elif self._repeat_last and self._last is not None:
            item = self._last
        else:
            raise AssertionError("сценарий FakeSource исчерпан")
        if isinstance(item, BaseException):
            raise item
        found = await item() if callable(item) else item
        now = self._clock.now() if self._clock is not None else T0
        return AvailabilitySnapshot(target.id, now, found, self.kind, self._latency_ms)

    async def close(self) -> None:
        self.closed += 1


class FakeAuthenticator:
    """Вход по сценарию: список исключений на первые попытки, затем успех."""

    def __init__(
        self,
        cookies: Sequence[SessionCookie] | None = None,
        failures: Sequence[BaseException] = (),
        delay: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.cookies = (
            list(cookies)
            if cookies is not None
            else [SessionCookie("simple_bpm_session", "TEST", "bpmgob.mec.gub.uy")]
        )
        self._failures = list(failures)
        self._delay = delay
        self.logins = 0
        self.concurrent = 0
        self.max_concurrent = 0

    async def login(self) -> list[SessionCookie]:
        self.logins += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self._delay is not None:
                await self._delay()
            if self._failures:
                raise self._failures.pop(0)
            return list(self.cookies)
        finally:
            self.concurrent -= 1


class FakeNotifier:
    """Канал уведомлений: запоминает отправки; может отказывать, тормозить и падать."""

    def __init__(
        self,
        channel: Channel,
        *,
        ok: bool = True,
        error: str = "",
        latency_ms: float = 1.0,
        delay: Callable[[], Awaitable[None]] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.channel = channel
        self.ok = ok
        self.error = error
        self.latency_ms = latency_ms
        self._delay = delay
        self.raises = raises
        self.sent: list[tuple[Alert, int]] = []
        self.warmups = 0
        self.closed = 0

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        if self._delay is not None:
            await self._delay()
        if self.raises is not None:
            raise self.raises
        self.sent.append((alert, attempt))
        error = "" if self.ok else (self.error or "сбой")
        return DeliveryResult(
            self.channel, self.ok, self.latency_ms, attempt, error, alert.incident_id
        )

    async def warmup(self) -> None:
        self.warmups += 1

    async def close(self) -> None:
        self.closed += 1

    @property
    def attempts(self) -> list[int]:
        return [attempt for _, attempt in self.sent]


def all_notifiers(**kwargs: Any) -> dict[Channel, FakeNotifier]:
    return {channel: FakeNotifier(channel, **kwargs) for channel in Channel}


# --- хранение в памяти ---


class MemoryKV:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value

    async def all(self) -> dict[str, str]:
        return dict(self.data)


class MemoryEventLog:
    def __init__(self) -> None:
        self.events: list[AppEvent] = []

    async def write(self, event: AppEvent) -> None:
        self.events.append(event)

    async def last(self, limit: int, min_level: str = "WARNING") -> list[AppEvent]:
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        wanted = [e for e in self.events if order.get(e.level, 0) >= order.get(min_level, 30)]
        return list(reversed(wanted))[:limit]


class MemoryStatsRepository:
    def __init__(self) -> None:
        self.saved: dict[datetime, PollStatsHour] = {}

    async def save_hour(self, stats: PollStatsHour) -> None:
        self.saved[stats.hour] = stats

    async def since(self, since: datetime) -> list[PollStatsHour]:
        return [h for hour, h in sorted(self.saved.items()) if hour >= since]


class MemoryIncidentRepository:
    def __init__(self) -> None:
        self.incidents: dict[int, Incident] = {}
        self.deliveries: list[tuple[DeliveryResult, datetime]] = []
        self._next_id = 1
        self.open_delay: Callable[[], Awaitable[None]] | None = None

    async def open(self, target_id: int, opened_at: datetime, slots: frozenset[Slot]) -> Incident:
        if self.open_delay is not None:
            await self.open_delay()
        incident = Incident(self._next_id, target_id, opened_at, slots)
        self.incidents[incident.id] = incident
        self._next_id += 1
        return incident

    async def update_slots(self, incident_id: int, slots: frozenset[Slot]) -> None:
        self.incidents[incident_id].slots = slots

    async def close(self, incident_id: int, closed_at: datetime) -> None:
        incident = self.incidents[incident_id]
        incident.status = IncidentStatus.CLOSED
        incident.closed_at = closed_at

    async def ack(self, incident_id: int, acked_at: datetime) -> None:
        incident = self.incidents[incident_id]
        if incident.acked_at is None:
            incident.acked_at = acked_at

    async def current(self, target_id: int) -> Incident | None:
        open_ones = [
            i
            for i in self.incidents.values()
            if i.target_id == target_id and i.status is IncidentStatus.OPEN
        ]
        return max(open_ones, key=lambda i: i.id, default=None)

    async def last(self, limit: int) -> list[Incident]:
        return sorted(self.incidents.values(), key=lambda i: -i.id)[:limit]

    async def record_delivery(self, result: DeliveryResult, at: datetime) -> None:
        self.deliveries.append((result, at))

    async def deliveries_for(self, incident_id: int) -> list[DeliveryResult]:
        return [r for r, _ in self.deliveries if r.incident_id == incident_id]


@dataclass
class RecordingJournal:
    """Журнал наблюдения в памяти: все записи доступны тесту."""

    checks: list[CheckRecord] = field(default_factory=list)
    events: list[tuple[str, str, str, dict[str, object]]] = field(default_factory=list)
    openings: list[OpeningRecord] = field(default_factory=list)
    statuses: list[dict[str, object]] = field(default_factory=list)
    quota_calls: int = 0
    quota_result: tuple[int, int] = (0, 0)
    bytes_used: int = 0

    def check(self, record: CheckRecord) -> None:
        self.checks.append(record)

    def event(self, code: str, message: str, level: str = "INFO", **data: object) -> None:
        self.events.append((code, message, level, dict(data)))

    def opening(self, record: OpeningRecord) -> None:
        self.openings.append(record)

    def status(self, snapshot: dict[str, object]) -> None:
        self.statuses.append(dict(snapshot))

    def enforce_quota(self) -> tuple[int, int]:
        self.quota_calls += 1
        return self.quota_result

    def size(self) -> int:
        return self.bytes_used

    def codes(self) -> list[str]:
        return [code for code, *_ in self.events]


# --- локальный HTTPS-сервер ---

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes


class LocalTlsServer:
    """HTTPS на 127.0.0.1 с сертификатом `trustme`. Использование:

        async with LocalTlsServer() as server:
            server.route("GET", "/api", handler)
            http = SafeHttp({"127.0.0.1"}, 5, ssl_context=server.client_ssl)
            async with http.request("GET", server.url("/api")) as response: ...

    Маршрут по умолчанию отвечает 404. `requests` — всё, что получил сервер.
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[RecordedRequest] = []
        self._runner: web.AppRunner | None = None
        self.port = 0
        self._ca = trustme.CA()
        self.client_ssl = ssl.create_default_context()
        self._ca.configure_trust(self.client_ssl)

    def route(self, method: str, path: str, handler: Handler) -> None:
        self._routes[(method.upper(), path)] = handler

    def respond(self, method: str, path: str, **kwargs: Any) -> None:
        """Постоянный ответ: `respond("GET", "/x", text="ok", status=200, headers={...})`."""

        async def handler(_: web.Request) -> web.StreamResponse:
            return web.Response(**kwargs)

        self.route(method, path, handler)

    def url(self, path: str = "/") -> str:
        return f"https://127.0.0.1:{self.port}{path}"

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            RecordedRequest(
                request.method,
                request.path,
                dict(request.query),
                dict(request.headers),
                await request.read(),
            )
        )
        handler = self._routes.get((request.method, request.path))
        if handler is None:
            return web.Response(status=404, text="not found")
        return await handler(request)

    async def __aenter__(self) -> LocalTlsServer:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        server_ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._ca.issue_cert("127.0.0.1").configure_cert(server_ssl)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=server_ssl)
        await site.start()
        sockets = site._server.sockets  # type: ignore[union-attr]
        self.port = int(sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
