"""ApiSource на локальном HTTPS-сервере: все ответы сайта и их различение (SPEC 1.3, 2.3, 7)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from tests.fakes import AGENDA_URL, T0, TARGET, FakeClock, LocalTlsServer
from yarl import URL

from rcbot.application.ports import SessionCookie
from rcbot.domain.errors import (
    ManualStepRequired,
    RateLimited,
    SessionExpired,
    SiteUnavailable,
    SourceNotConfigured,
    UnexpectedResponse,
)
from rcbot.domain.models import Slot, SourceKind
from rcbot.infrastructure.gubuy import api_source
from rcbot.infrastructure.gubuy.api_source import ApiSource
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR

Factory = Callable[..., ApiSource]

LOGIN_HTML = "<html><h1>Elige un método para identificarte en ID Uruguay</h1></html>"
STEP1_HTML = "<html><b>Verificación de seguridad</b> ¿Qué número está antes del 1?</html>"
BLOCK_HTML = "<html>The requested URL was rejected. Please consult with your administrator.</html>"


@pytest.fixture
async def make(
    tls_server: LocalTlsServer, tmp_path: Path, clock: FakeClock
) -> AsyncIterator[Factory]:
    created: list[ApiSource] = []

    def factory(*, timeout: float = 5.0, api_url: str | None = None) -> ApiSource:
        # unsafe=True: cookies для IP-адреса 127.0.0.1 (у настоящего сайта — имя хоста)
        http = SafeHttp(
            {"127.0.0.1"},
            timeout,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            ssl_context=tls_server.client_ssl,
        )
        url = tls_server.url("/api") if api_url is None else api_url
        source = ApiSource(http, url, AGENDA_URL, tmp_path / "quarantine", clock)
        created.append(source)
        return source

    yield factory
    for source in created:
        await source.close()


def quarantined(tmp_path: Path) -> list[dict[str, object]]:
    directory = tmp_path / "quarantine"
    return [json.loads(f.read_text("utf-8")) for f in sorted(directory.glob("*.json"))]


def answer(server: LocalTlsServer, **kwargs: object) -> None:
    server.respond("GET", "/api", **kwargs)


# --- успешный разбор (парсер ответа появится после разведки: подставляем свой) ---


async def test_разобранный_ответ_становится_снимком(
    make: Factory, tls_server: LocalTlsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer(tls_server, text='{"dias": [23, 24]}', content_type="application/json")
    seen: list[object] = []

    def parser(payload: object) -> frozenset[Slot]:
        seen.append(payload)
        return frozenset({Slot(date(2027, 2, 23)), Slot(date(2027, 2, 24))})

    monkeypatch.setattr(api_source, "parse_disponibilidades", parser)
    snapshot = await make().fetch(TARGET)
    assert seen == [{"dias": [23, 24]}]
    assert snapshot.source is SourceKind.API
    assert snapshot.target_id == TARGET.id
    assert snapshot.observed_at == T0
    assert {s.day.day for s in snapshot.slots} == {23, 24}
    assert snapshot.latency_ms >= 0


async def test_ответ_ровно_в_предел_разбирается(
    make: Factory, tls_server: LocalTlsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = '{"dias": [1]}'
    monkeypatch.setattr(api_source, "MAX_RESPONSE_BYTES", len(body))
    monkeypatch.setattr(api_source, "parse_disponibilidades", lambda _: frozenset())
    answer(tls_server, text=body, content_type="application/json")
    assert (await make().fetch(TARGET)).slots == frozenset()


async def test_content_length_больше_предела_это_неожиданный_ответ_без_чтения_тела(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_source, "MAX_RESPONSE_BYTES", 64)
    answer(tls_server, text='{"dias": [' + "1, " * 100 + "1]}", content_type="application/json")
    with pytest.raises(UnexpectedResponse, match="больше 64 байт"):
        await make().fetch(TARGET)
    [record] = quarantined(tmp_path)
    assert record["body"] == ""  # тело не читалось


async def test_потоковый_ответ_без_длины_больше_предела_обрывается_на_пределе(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_source, "MAX_RESPONSE_BYTES", 1000)
    sent: list[int] = []

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        response.enable_chunked_encoding()
        await response.prepare(request)
        try:
            for _ in range(200):
                await response.write(b"[" * 100)
                sent.append(100)
        except ConnectionError:
            pass
        return response

    tls_server.route("GET", "/api", stream)
    with pytest.raises(UnexpectedResponse, match="больше 1000 байт"):
        await make().fetch(TARGET)
    [record] = quarantined(tmp_path)
    assert record["body"] == "[" * 1000  # скрипт/JSON: начало тела через редактор, не больше 1000


@pytest.mark.parametrize(
    ("raw", "charset", "expected"),
    [
        ("Año".encode("latin-1"), "latin-1", "Año"),
        ("Año".encode(), None, "Año"),
        ("Año".encode(), "no-such-charset", "Año"),
        (b"\xff\xfe", None, "��"),
    ],
)
def test_кодировка_тела_как_у_response_text(raw: bytes, charset: str | None, expected: str) -> None:
    assert api_source._decode(raw, charset) == expected


async def test_пустой_ответ_парсера_это_нет_мест_а_не_ошибка(
    make: Factory, tls_server: LocalTlsServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer(tls_server, text="[]", content_type="application/json", charset="utf-8")
    monkeypatch.setattr(api_source, "parse_disponibilidades", lambda _: frozenset())
    assert (await make().fetch(TARGET)).slots == frozenset()


async def test_заглушка_разбора_отказывает_понятно_и_кладёт_снимок_в_карантин(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    answer(tls_server, text='{"dias": [1]}', content_type="application/json")
    with pytest.raises(UnexpectedResponse, match="ещё не разобран") as info:
        await make().fetch(TARGET)
    assert "в карантине" in str(info.value)
    [record] = quarantined(tmp_path)
    assert record["status"] == 200
    assert record["source"] == "api"
    assert record["body"] == '{"dias": [1]}'


async def test_ошибка_парсера_значения_тоже_уходит_в_карантин(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer(tls_server, text='{"dias": "x"}', content_type="application/json")

    def parser(_: object) -> frozenset[Slot]:
        raise ValueError("день вне диапазона")

    monkeypatch.setattr(api_source, "parse_disponibilidades", parser)
    with pytest.raises(UnexpectedResponse, match="день вне диапазона"):
        await make().fetch(TARGET)
    assert len(quarantined(tmp_path)) == 1


# --- заголовки и cookies ---


async def test_уходят_заголовки_xhr_и_referer(make: Factory, tls_server: LocalTlsServer) -> None:
    answer(tls_server, text="{}", content_type="application/json")
    with pytest.raises(UnexpectedResponse):
        await make().fetch(TARGET)
    headers = {k.lower(): v for k, v in tls_server.requests[0].headers.items()}
    assert headers["x-requested-with"] == "XMLHttpRequest"
    assert headers["referer"] == AGENDA_URL
    assert tls_server.requests[0].method == "GET"


async def test_cookies_из_use_cookies_реально_отправляются(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    answer(tls_server, text="{}", content_type="application/json")
    source = make()
    source.use_cookies(
        [
            SessionCookie("simple_bpm_session", "TEST-VALUE-1", "127.0.0.1"),
            SessionCookie("TS0191f197", "TEST-VALUE-2", "127.0.0.1"),
        ]
    )
    with pytest.raises(UnexpectedResponse):
        await source.fetch(TARGET)
    cookie = tls_server.requests[0].headers["Cookie"]
    assert "simple_bpm_session=" in cookie
    assert "TS0191f197=" in cookie


async def test_use_cookies_заменяет_прежние_а_не_дополняет(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    answer(tls_server, text="{}", content_type="application/json")
    source = make()
    source.use_cookies([SessionCookie("old_cookie", "1", "127.0.0.1")])
    source.use_cookies([SessionCookie("new_cookie", "2", "127.0.0.1")])
    with pytest.raises(UnexpectedResponse):
        await source.fetch(TARGET)
    cookie = tls_server.requests[0].headers["Cookie"]
    assert "new_cookie=" in cookie
    assert "old_cookie" not in cookie


async def test_без_cookies_заголовок_cookie_не_уходит(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    answer(tls_server, text="{}", content_type="application/json")
    with pytest.raises(UnexpectedResponse):
        await make().fetch(TARGET)
    assert "Cookie" not in tls_server.requests[0].headers


async def test_cookie_родительского_домена_достаётся_поддомену() -> None:
    # Playwright отдаёт cookies с точкой в начале домена (".mec.gub.uy"): они действуют и на
    # bpmgob.mec.gub.uy; потеряв их, бот зря считал бы сессию истёкшей
    jar = aiohttp.CookieJar()
    http = SafeHttp({"bpmgob.mec.gub.uy"}, 5, cookie_jar=jar)
    source = ApiSource(http, "https://bpmgob.mec.gub.uy/api", AGENDA_URL, Path("q"), FakeClock())
    source.use_cookies(
        [
            SessionCookie("parent", "1", ".mec.gub.uy"),
            SessionCookie("exact", "2", ".bpmgob.mec.gub.uy"),
            SessionCookie("host", "3", "bpmgob.mec.gub.uy"),
        ]
    )
    sent = jar.filter_cookies(URL("https://bpmgob.mec.gub.uy/api"))
    assert {"parent", "exact", "host"} == set(sent)
    # чужой хост эти cookies не получает
    assert not jar.filter_cookies(URL("https://mi.iduruguay.gub.uy/"))
    await source.close()


# --- переадресации ---


@pytest.mark.parametrize(
    "location",
    [
        "/autenticacion/login?redirect=x",
        "https://bpmgob.mec.gub.uy/autenticacion/login",
        "https://mi.iduruguay.gub.uy/login?process_state=TEST",
        "https://MI.IDURUGUAY.GUB.UY/login",
    ],
)
@pytest.mark.parametrize("status", [301, 302, 303, 307])
async def test_переадресация_на_вход_это_истёкшая_сессия(
    make: Factory,
    tls_server: LocalTlsServer,
    tmp_path: Path,
    location: str,
    status: int,
) -> None:
    answer(tls_server, status=status, headers={"Location": location})
    with pytest.raises(SessionExpired):
        await make().fetch(TARGET)
    assert quarantined(tmp_path) == []  # ожидаемое событие: снимок не нужен
    assert len(tls_server.requests) == 1  # за переадресацией бот не идёт


@pytest.mark.parametrize(
    "location",
    [
        "/otra/pagina",
        "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/2",
        "https://evil.example/autenticacion",
        "https://evil.example/?next=https://mi.iduruguay.gub.uy/login",
        "https://evil.example/mi.iduruguay.gub.uy",
        "",
    ],
)
async def test_переадресация_в_другое_место_это_карантин(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path, location: str
) -> None:
    headers = {"Location": location} if location else {}
    answer(tls_server, status=302, headers=headers)
    with pytest.raises(UnexpectedResponse, match="переадресация"):
        await make().fetch(TARGET)
    [record] = quarantined(tmp_path)
    assert record["status"] == 302


# --- статусы ---


@pytest.mark.parametrize("status", [403, 429])
async def test_403_и_429_это_ограничение(
    make: Factory, tls_server: LocalTlsServer, status: int
) -> None:
    answer(tls_server, status=status, text="Too many requests")
    with pytest.raises(RateLimited, match=str(status)):
        await make().fetch(TARGET)


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
async def test_5xx_это_сайт_недоступен(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path, status: int
) -> None:
    answer(tls_server, status=status, text="ошибка")
    with pytest.raises(SiteUnavailable, match=str(status)):
        await make().fetch(TARGET)
    assert quarantined(tmp_path) == []


async def test_статус_499_это_не_5xx_а_неожиданный_ответ(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    answer(tls_server, status=499, text="<html>x</html>", content_type="text/html")
    with pytest.raises(UnexpectedResponse):
        await make().fetch(TARGET)
    assert len(quarantined(tmp_path)) == 1


# --- HTML вместо JSON ---


async def test_страница_входа_вместо_json_это_истёкшая_сессия(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    answer(tls_server, text=LOGIN_HTML, content_type="text/html")
    with pytest.raises(SessionExpired, match="страница входа"):
        await make().fetch(TARGET)


async def test_шаг_один_вместо_json_зовёт_владельца(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    answer(tls_server, text=STEP1_HTML, content_type="text/html")
    with pytest.raises(ManualStepRequired):
        await make().fetch(TARGET)


async def test_страница_блокировки_вместо_json_это_ограничение(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    answer(tls_server, text=BLOCK_HTML, content_type="text/html")
    with pytest.raises(RateLimited, match="блокировки"):
        await make().fetch(TARGET)


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        ("<html><body>Hola</body></html>", "text/html; charset=utf-8"),
        ('{"dias": []}', "text/plain"),  # JSON под чужим типом содержимого не принимается
        ('{"dias": []}', ""),
        ("", "text/html"),
        ("Привет", "application/octet-stream"),
    ],
)
async def test_прочее_вместо_json_это_карантин(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path, body: str, content_type: str
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    answer(tls_server, body=body.encode(), headers=headers)
    with pytest.raises(UnexpectedResponse, match="HTML вместо ответа API"):
        await make().fetch(TARGET)
    assert len(quarantined(tmp_path)) == 1


@pytest.mark.parametrize("body", ['{"dias": [', "", "не json", "{'a': 1}", "NaN,", "\x00"])
async def test_битый_json_это_карантин(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path, body: str
) -> None:
    answer(tls_server, text=body, content_type="application/json")
    with pytest.raises(UnexpectedResponse):
        await make().fetch(TARGET)
    assert len(quarantined(tmp_path)) == 1


async def test_неверная_кодировка_ответа_не_роняет_разбор(
    make: Factory, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    answer(tls_server, body=b"\xff\xfe\x00 \xc3", headers={"Content-Type": "text/html"})
    with pytest.raises(UnexpectedResponse):
        await make().fetch(TARGET)
    assert len(quarantined(tmp_path)) == 1


@pytest.mark.parametrize(
    ("body", "content_type", "stored"),
    [
        ("<p>УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА, simple_bpm_session=abc123</p>", "text/html", False),
        ('{"nota": "УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА simple_bpm_session=abc123"}', "application/json", True),
    ],
    ids=["страница-не-пишется", "json-через-редактор"],
)
async def test_карантин_без_cookies_и_с_вырезанными_секретами(
    make: Factory,
    tls_server: LocalTlsServer,
    tmp_path: Path,
    body: str,
    content_type: str,
    stored: bool,
) -> None:
    REDACTOR.register("УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА")
    answer(
        tls_server,
        text=body,
        content_type=content_type,
        headers={"Set-Cookie": "simple_bpm_session=SERVER-TEST; Path=/"},
    )
    with pytest.raises(UnexpectedResponse):
        await make().fetch(TARGET)
    file = next((tmp_path / "quarantine").glob("*.json"))
    text = file.read_text("utf-8")
    assert "УСЛОВНЫЙ-СЕКРЕТ-ТЕСТА" not in text
    assert "abc123" not in text
    assert "SERVER-TEST" not in text
    assert "Set-Cookie" not in text
    record = json.loads(text)
    if stored:
        assert MASK in record["body"]
    else:
        assert record["body"]["omitted"] is True
        assert record["body"]["chars"] == len(body)


# --- сбои сети ---


async def test_таймаут_это_сайт_недоступен(make: Factory, tls_server: LocalTlsServer) -> None:
    async def slow(_: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.Response(text="поздно")

    tls_server.route("GET", "/api", slow)
    with pytest.raises(SiteUnavailable, match="TimeoutError"):
        await make(timeout=0.2).fetch(TARGET)


async def test_обрыв_соединения_это_сайт_недоступен(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    async def drop(request: web.Request) -> web.Response:
        request.transport.abort()  # type: ignore[union-attr]
        return web.Response()

    tls_server.route("GET", "/api", drop)
    with pytest.raises(SiteUnavailable, match="API недоступен"):
        await make().fetch(TARGET)


async def test_обрыв_посреди_тела_это_сайт_недоступен(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    async def cut(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Length": "1000"})
        await response.prepare(request)
        await response.write(b"{")
        request.transport.abort()  # type: ignore[union-attr]
        return response

    tls_server.route("GET", "/api", cut)
    with pytest.raises(SiteUnavailable):
        await make().fetch(TARGET)


async def test_недоступный_порт_это_сайт_недоступен(make: Factory) -> None:
    source = make(api_url="https://127.0.0.1:1/api")
    with pytest.raises(SiteUnavailable):
        await source.fetch(TARGET)


async def test_сообщение_об_ошибке_сети_без_адреса(make: Factory) -> None:
    with pytest.raises(SiteUnavailable) as info:
        await make(api_url="https://127.0.0.1:1/api?token=ЗНАЧЕНИЕ").fetch(TARGET)
    assert "ЗНАЧЕНИЕ" not in str(info.value)
    assert "127.0.0.1" not in str(info.value)


async def test_адрес_вне_списка_не_запрашивается(make: Factory, tls_server: LocalTlsServer) -> None:
    from rcbot.domain.errors import HostNotAllowed

    source = make(api_url="https://evil.example/api")
    with pytest.raises(HostNotAllowed):
        await source.fetch(TARGET)
    assert tls_server.requests == []


# --- настройка и закрытие ---


async def test_источник_без_адреса_не_настроен_и_не_ходит_в_сеть(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    source = make(api_url="")
    assert source.configured is False
    with pytest.raises(SourceNotConfigured, match="RCBOT_API_URL"):
        await source.fetch(TARGET)
    assert tls_server.requests == []


async def test_настроенный_источник_и_его_вид(make: Factory) -> None:
    source = make()
    assert source.configured is True
    assert source.kind is SourceKind.API


async def test_close_закрывает_сессию_и_повторяем(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    answer(tls_server, text="{}", content_type="application/json")
    source = make()
    with pytest.raises(UnexpectedResponse):
        await source.fetch(TARGET)
    session = source._http.session
    await source.close()
    await source.close()
    assert session.closed
    # после закрытия источник снова работает (новая сессия): повторный вход бота не ломается
    with pytest.raises(UnexpectedResponse):
        await source.fetch(TARGET)


async def test_один_источник_держит_одну_сессию_на_все_проверки(
    make: Factory, tls_server: LocalTlsServer
) -> None:
    answer(tls_server, text="{}", content_type="application/json")
    source = make()
    sessions = set()
    for _ in range(3):
        with pytest.raises(UnexpectedResponse):
            await source.fetch(TARGET)
        sessions.add(id(source._http.session))
    assert len(sessions) == 1
