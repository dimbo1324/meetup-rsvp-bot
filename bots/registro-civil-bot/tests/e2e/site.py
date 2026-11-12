"""Фальшивые внешние стороны для сквозных тестов: «bpmgob» и сервер каналов (ntfy, CallMeBot).

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

from rcbot.application.ports import SessionCookie
from rcbot.domain.errors import UnexpectedResponse
from rcbot.domain.models import Slot
from tests.e2e.clock import StandClock
from tests.fakes import FakeAuthenticator, LocalTlsServer

API_PATH = "/etapas/agenda_sae_api_disponibilidades"
LOGIN_LOCATION = "https://bpmgob.mec.gub.uy/autenticacion/login"
SESSION_COOKIE = "simple_bpm_session"

Reply = Callable[[web.Request], Awaitable[web.StreamResponse]]

STEP1_HTML = (
    "<html><body><h1>Paso 1 de 3</h1><div>Verificación de seguridad</div>"
    "<a>Solicitar otra pregunta</a></body></html>"
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


def json_text(text: str) -> Reply:
    """Тело как есть с типом JSON: можно отдать и невалидный JSON, и неожиданную структуру."""

    async def reply(_: web.Request) -> web.StreamResponse:
        return web.Response(text=text, content_type="application/json")

    return reply


def redirect_to_login() -> Reply:
    async def reply(_: web.Request) -> web.StreamResponse:
        return web.Response(status=302, headers={"Location": LOGIN_LOCATION})

    return reply


def echo_cookies_html() -> Reply:
    """Страница-мусор, которая эхом возвращает заголовок Cookie и «секреты»: проверка очистки."""

    async def reply(request: web.Request) -> web.StreamResponse:
        body = (
            "<html><body>Error inesperado. "
            f"Cookie: {request.headers.get('Cookie', '')}\n"
            "Set-Cookie: TS0191f197=deadbeefcafe0123; Path=/\n"
            "password=hunter22-test token=abcd1234efgh phone +598 99 123 456\n"
            "documento 123456789012 owner.test@example.org</body></html>"
        )
        return web.Response(status=200, text=body, content_type="text/html")

    return reply


def parse_test_payload(payload: object) -> frozenset[Slot]:
    """Разбор тестового формата `{"slots": ["2027-02-23T10:30", "2027-03-01"]}`.

    Настоящий разбор появится после разведки (этап 3); до этого `parse_disponibilidades` всегда
    отказывает. Стенд подменяет его этим разбором, чтобы проверить весь остальной путь. Как и
    настоящему, неожиданная структура ему — `UnexpectedResponse`.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("slots"), list):
        raise UnexpectedResponse("в ответе нет списка slots")
    found: set[Slot] = set()
    for item in payload["slots"]:
        if not isinstance(item, str):
            raise UnexpectedResponse("слот не строка")
        try:
            day, _, clock = item.partition("T")
            found.add(Slot(date.fromisoformat(day), dtime.fromisoformat(clock) if clock else None))
        except ValueError as error:
            raise UnexpectedResponse("слот не разобран") from error
    return frozenset(found)


@dataclass
class SiteCall:
    mono: float
    cookie: str
    perf: float


@dataclass
class FakeSite:
    """«bpmgob»: отдаёт календарь тем, у кого действующая сессия; остальных — на страницу входа.

    `script` — очередь ответов, которые отдаются раньше обычной логики (ошибки, мусор, шаг 1).
    """

    server: LocalTlsServer
    clock: StandClock
    slots: list[str] = field(default_factory=list)
    valid_sessions: set[str] = field(default_factory=set)
    script: deque[Reply] = field(default_factory=deque)
    calls: list[SiteCall] = field(default_factory=list)
    release_hang: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.server.route("GET", API_PATH, self._api)

    @property
    def api_url(self) -> str:
        return self.server.url(API_PATH)

    def push(self, *replies: Reply, times: int = 1) -> None:
        for _ in range(times):
            self.script.extend(replies)

    def expire_sessions(self) -> None:
        self.valid_sessions.clear()

    def hang(self) -> Reply:
        """Ответ, который не приходит, пока тест не отпустит его (или не кончится тест)."""

        async def reply(_: web.Request) -> web.StreamResponse:
            await self.release_hang.wait()
            return web.Response(status=200, text="{}", content_type="application/json")

        return reply

    def _has_session(self, request: web.Request) -> bool:
        for part in request.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value in self.valid_sessions:
                return True
        return False

    async def _api(self, request: web.Request) -> web.StreamResponse:
        self.calls.append(
            SiteCall(self.clock.monotonic(), request.headers.get("Cookie", ""), time.perf_counter())
        )
        if self.script:
            return await self.script.popleft()(request)
        if not self._has_session(request):
            return web.Response(status=302, headers={"Location": LOGIN_LOCATION})
        return web.json_response({"slots": list(self.slots)})


class SiteAuthenticator(FakeAuthenticator):
    """Вход, после которого «сайт» узнаёт новую сессию; значение cookie у каждого входа своё."""

    def __init__(self, site: FakeSite, *, domain: str = "127.0.0.1", **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._site = site
        self._domain = domain
        self.issued: list[str] = []

    async def login(self) -> list[SessionCookie]:
        await super().login()  # сценарные отказы и задержки FakeAuthenticator
        value = f"SESSION-{self.logins}-{len(self.issued)}"
        self.issued.append(value)
        self._site.valid_sessions.add(value)
        return [SessionCookie(SESSION_COOKIE, value, self._domain)]


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
