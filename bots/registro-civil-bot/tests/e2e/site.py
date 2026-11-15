"""Фальшивые внешние стороны для сквозных тестов: страница записи SAE и сервер каналов.

Оба работают на настоящем локальном HTTPS-сервере (`LocalTlsServer`): бот ходит к ним по реальным
сокетам и TLS, а не через подмену клиента. Живых адресов здесь нет.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from datetime import time as dtime

from aiohttp import web

from rcbot.domain.models import Slot
from tests.e2e.clock import StandClock
from tests.fakes import LocalTlsServer

SAE_PATH = "/sae/agendarReserva/Paso1.xhtml"

Reply = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Страница записи без мест — как в снимке живой страницы (надпись «нет мест» и выбор офиса)
EMPTY_PAGE = (
    "<html><body><h1>Inscripción Extranjeros</h1>"
    '<div id="form:warnSinCupos"><h4>En la oficina seleccionada no hay cupos disponibles</h4></div>'
    "<fieldset><span>Ubicación</span><input type=radio checked></fieldset></body></html>"
)
# Страница записи с местами: надписи нет; даты — в синтетическом блоке для тестового разбора
OPEN_PAGE = (
    "<html><body><h1>Inscripción Extranjeros</h1>"
    "<fieldset><span>Ubicación</span><input type=radio checked></fieldset>"
    '<div class="calendario">{dates}</div></body></html>'
)
BLOCKED_HTML = "<html><body>The requested URL was rejected. Please consult your administrator."


# --- ответы сайта ---


def status(code: int, text: str = "") -> Reply:
    async def reply(_: web.Request) -> web.StreamResponse:
        return web.Response(status=code, text=text, content_type="text/plain")

    return reply


def html(text: str, code: int = 200) -> Reply:
    async def reply(_: web.Request) -> web.StreamResponse:
        return web.Response(status=code, text=text, content_type="text/html")

    return reply


def redirect(location: str = "https://evil.example/") -> Reply:
    async def reply(_: web.Request) -> web.StreamResponse:
        return web.Response(status=302, headers={"Location": location})

    return reply


SITE_COOKIE = "SESSION-SAE-0001"


def echo_secrets_html() -> Reply:
    """Чужая страница с cookie сайта и «секретами» в теле: проверка очистки снимков и логов."""

    async def reply(_: web.Request) -> web.StreamResponse:
        body = (
            "<html><body>Error inesperado.\n"
            "Set-Cookie: TS0191f197=deadbeefcafe0123; Path=/\n"
            "password=hunter22-test token=abcd1234efgh phone +598 99 123 456\n"
            "documento 123456789012 owner.test@example.org</body></html>"
        )
        headers = {"Set-Cookie": f"JSESSIONID={SITE_COOKIE}; Path=/; Secure"}
        return web.Response(status=200, text=body, content_type="text/html", headers=headers)

    return reply


def parse_test_dates(items: list[str]) -> frozenset[Slot]:
    """Тестовый разбор дат: `["2027-02-23T10:30", "2027-03-01"]` → слоты.

    Настоящего разбора дат страницы SAE ещё нет (R12: ждём снимок страницы с местами). Стенд
    подставляет этот разбор, чтобы проверить весь остальной путь тревоги с датами.
    """
    found: set[Slot] = set()
    for item in items:
        day, _, clock = item.partition("T")
        found.add(Slot(date.fromisoformat(day), dtime.fromisoformat(clock) if clock else None))
    return frozenset(found)


@dataclass
class SiteCall:
    mono: float
    perf: float


@dataclass
class FakeSite:
    """Страница записи SAE: без мест — надпись «нет мест», с местами — страница без неё.

    `script` — очередь ответов, которые отдаются раньше обычной логики (ошибки, мусор, зависание).
    `served` — даты, показанные последней страницей с местами (их читает тестовый разбор).
    """

    server: LocalTlsServer
    clock: StandClock
    slots: list[str] = field(default_factory=list)
    script: deque[Reply] = field(default_factory=deque)
    calls: list[SiteCall] = field(default_factory=list)
    served: list[str] = field(default_factory=list)
    release_hang: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.server.route("GET", SAE_PATH, self._page)

    @property
    def url(self) -> str:
        return self.server.url(SAE_PATH)

    def push(self, *replies: Reply, times: int = 1) -> None:
        for _ in range(times):
            self.script.extend(replies)

    def hang(self) -> Reply:
        """Ответ, который не приходит, пока тест не отпустит его (или не кончится тест)."""

        async def reply(_: web.Request) -> web.StreamResponse:
            await self.release_hang.wait()
            return web.Response(status=200, text=EMPTY_PAGE, content_type="text/html")

        return reply

    async def _page(self, request: web.Request) -> web.StreamResponse:
        self.calls.append(SiteCall(self.clock.monotonic(), time.perf_counter()))
        if self.script:
            return await self.script.popleft()(request)
        self.served = list(self.slots)
        if not self.slots:
            return web.Response(text=EMPTY_PAGE, content_type="text/html")
        page = OPEN_PAGE.format(dates=" ".join(self.slots))
        return web.Response(text=page, content_type="text/html")


# --- сервер каналов ---


@dataclass
class ChannelRequest:
    kind: str  # ntfy | callmebot | health
    perf: float
    peer_port: int
    body: dict[str, object]
    query: dict[str, str]
    headers: dict[str, str]


@dataclass
class ChannelsSite:
    """Один сервер в роли ntfy и CallMeBot: запоминает запросы и момент их получения."""

    server: LocalTlsServer
    requests: list[ChannelRequest] = field(default_factory=list)
    ntfy_status: int = 200
    call_replies: deque[tuple[int, str]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.server.route("POST", "/", self._ntfy)
        self.server.route("GET", "/v1/health", self._health)
        self.server.route("GET", "/start.php", self._call)

    @property
    def ntfy_server(self) -> str:
        return self.server.url("").rstrip("/")

    @property
    def callmebot_url(self) -> str:
        return self.server.url("/start.php")

    def of(self, kind: str) -> list[ChannelRequest]:
        return [r for r in self.requests if r.kind == kind]

    def _note(self, request: web.Request, kind: str, body: dict[str, object] | None = None) -> None:
        transport = request.transport
        peer = transport.get_extra_info("peername") if transport is not None else None
        self.requests.append(
            ChannelRequest(
                kind,
                time.perf_counter(),
                int(peer[1]) if peer else 0,
                body or {},
                dict(request.query),
                dict(request.headers),
            )
        )

    async def _ntfy(self, request: web.Request) -> web.StreamResponse:
        raw = await request.read()
        self._note(request, "ntfy", json.loads(raw) if raw else {})
        return web.json_response({"id": "x"}, status=self.ntfy_status)

    async def _health(self, request: web.Request) -> web.StreamResponse:
        self._note(request, "health")
        return web.json_response({"healthy": True})

    async def _call(self, request: web.Request) -> web.StreamResponse:
        self._note(request, "callmebot")
        code, text = self.call_replies.popleft() if self.call_replies else (200, "Call started")
        return web.Response(status=code, text=text)
