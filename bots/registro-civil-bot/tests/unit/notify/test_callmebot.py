"""CallMeBotNotifier (SPEC 2.4, D8): запрос, разбор ответа, английский повтор, секреты."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web

from rcbot.application.messages import CALL_MAX_CHARS, CALL_TEXT_EN, CALL_TEXT_RU
from rcbot.domain.errors import HostNotAllowed
from rcbot.domain.models import Alert, AlertKind, Channel
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.notify import callmebot
from rcbot.infrastructure.notify.callmebot import FALLBACK_LANG, CallMeBotNotifier
from tests.fakes import LocalTlsServer
from tests.unit.notify.helpers import (
    PRIVATE_MARKERS,
    RaisingHttp,
    all_alerts,
    hang_up,
    http_for,
    stall,
)

USER = "usuario_secreto_77"
RU = "ru-RU-Standard-A"
PATH = "/start.php"


@pytest.fixture
def server(tls_server: LocalTlsServer, monkeypatch: pytest.MonkeyPatch) -> LocalTlsServer:
    """Сервис CallMeBot подменён локальным сервером; настоящий api.callmebot.com недостижим."""
    monkeypatch.setattr(callmebot, "API_URL", tls_server.url(PATH))
    return tls_server


def make(server: LocalTlsServer, user: str = USER, lang: str = RU, timeout: float = 5.0):
    return CallMeBotNotifier(http_for(server, timeout), user, lang)


def scripted(*answers: tuple[int, str]) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """Ответы по очереди; после исчерпания повторяется последний."""
    queue = list(answers)

    async def handler(_: web.Request) -> web.StreamResponse:
        status, text = queue.pop(0) if len(queue) > 1 else queue[0]
        return web.Response(status=status, text=text)

    return handler


# --- параметры запроса ---


async def test_параметры_user_text_lang_rpt(server: LocalTlsServer) -> None:
    server.respond("GET", PATH, text="Calling...")
    channel = make(server)
    result = await channel.send(all_alerts()["slots_alert"], 1)
    await channel.close()
    assert result.ok and result.channel is Channel.CALL and result.error == ""
    request = server.requests[0]
    assert request.method == "GET" and request.path == PATH
    assert request.query == {
        "user": f"@{USER}",
        "text": CALL_TEXT_RU,
        "lang": RU,
        "rpt": "2",
    }


@pytest.mark.parametrize("given", [USER, f"@{USER}"])
async def test_собака_в_имени_ровно_одна(server: LocalTlsServer, given: str) -> None:
    server.respond("GET", PATH, text="ok")
    channel = make(server, user=given)
    await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert server.requests[0].query["user"] == f"@{USER}"


async def test_русский_текст_и_спецсимволы_доходят_без_искажений(server: LocalTlsServer) -> None:
    server.respond("GET", PATH, text="ok")
    text = "Привет & пока=да? #1 +плюс %20 «ёлка»"
    channel = make(server)
    await channel.send(Alert(AlertKind.TEST, "т", call_text=text), 1)
    await channel.close()
    assert server.requests[0].query["text"] == text


async def test_текст_усекается_до_256_символов(server: LocalTlsServer) -> None:
    server.respond("GET", PATH, text="ok")
    channel = make(server)
    await channel.send(Alert(AlertKind.TEST, "т", call_text="я" * 400), 1)
    await channel.close()
    assert server.requests[0].query["text"] == "я" * CALL_MAX_CHARS == "я" * 256


async def test_текст_ровно_256_не_трогается(server: LocalTlsServer) -> None:
    server.respond("GET", PATH, text="ok")
    channel = make(server)
    await channel.send(Alert(AlertKind.TEST, "т", call_text="ж" * 256), 1)
    await channel.close()
    assert server.requests[0].query["text"] == "ж" * 256


async def test_английский_повтор_тоже_не_длиннее_256(server: LocalTlsServer) -> None:
    server.respond("GET", PATH, status=500, text="плохо")
    channel = make(server)
    await channel.send(all_alerts()["slots_alert"], 1)
    await channel.close()
    assert all(len(r.query["text"]) <= 256 for r in server.requests)


@pytest.mark.parametrize(
    ("call_text", "push_text", "ожидается"),
    [
        ("Звонок", "Push", "Звонок"),
        ("", "Push", "Push"),
        ("", "", "Внимание, есть новости от бота."),
    ],
)
async def test_выбор_текста_звонка(
    server: LocalTlsServer, call_text: str, push_text: str, ожидается: str
) -> None:
    server.respond("GET", PATH, text="ok")
    channel = make(server)
    alert = Alert(AlertKind.TEST, "ПОЛНЫЙ ТЕКСТ", call_text=call_text, push_text=push_text)
    await channel.send(alert, 1)
    await channel.close()
    assert server.requests[0].query["text"] == ожидается


@pytest.mark.parametrize("name", list(all_alerts()))
async def test_в_звонок_не_попадают_заявка_и_ссылки(server: LocalTlsServer, name: str) -> None:
    server.respond("GET", PATH, text="ok")
    channel = make(server)
    await channel.send(all_alerts()[name], 1)
    await channel.close()
    spoken = server.requests[0].query["text"]
    for marker in PRIVATE_MARKERS:
        assert marker not in spoken, f"{name}: «{marker}» уйдёт в голосовой звонок"


# --- разбор ответа сервиса ---


@pytest.mark.parametrize("body", ["Calling...", "OK", "", "Call placed", "Звоним"])
async def test_200_без_маркеров_ошибки_это_успех(server: LocalTlsServer, body: str) -> None:
    server.respond("GET", PATH, text=body)
    channel = make(server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert result.ok and result.error == ""
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        "Error: user not found",
        "ERROR",
        "You are Not Authorized",
        "NOT AUTHORIZED",
        "Invalid parameter",
        "INVALID",
        "You need to start the bot first",
        "YOU NEED TO authorize",
        "<html>Some Error occurred</html>",
    ],
)
async def test_маркеры_ошибки_в_теле_любого_регистра(server: LocalTlsServer, body: str) -> None:
    server.respond("GET", PATH, text=body)
    channel = make(server, lang=FALLBACK_LANG)  # без повтора: ровно один запрос
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and result.error == "сервис ответил ошибкой"
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [201, 204, 400, 401, 404, 429, 500, 503])
async def test_не_200_это_ошибка_с_кодом(server: LocalTlsServer, status: int) -> None:
    server.respond("GET", PATH, status=status, text="Calling...")
    channel = make(server, lang=FALLBACK_LANG)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and result.error == f"HTTP {status}"


async def test_тело_не_в_utf8_не_ломает_разбор(server: LocalTlsServer) -> None:
    async def handler(_: web.Request) -> web.StreamResponse:
        return web.Response(body=b"\xff\xfe\x00 Calling", content_type="text/plain")

    server.route("GET", PATH, handler)
    channel = make(server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert result.ok


# --- сеть ---


async def test_таймаут_это_ошибка_без_исключения(server: LocalTlsServer) -> None:
    server.route("GET", PATH, stall)
    channel = make(server, lang=FALLBACK_LANG, timeout=0.15)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and result.error == "TimeoutError"


async def test_обрыв_соединения_это_ошибка_без_исключения(server: LocalTlsServer) -> None:
    server.route("GET", PATH, hang_up)
    channel = make(server, lang=FALLBACK_LANG)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and result.error


@pytest.mark.parametrize(
    "error", [aiohttp.ClientConnectionError("x"), aiohttp.ServerDisconnectedError(), TimeoutError()]
)
async def test_сетевая_ошибка_в_обеих_попытках(error: BaseException) -> None:
    http = RaisingHttp(error)
    channel = CallMeBotNotifier(http, USER, RU)  # type: ignore[arg-type]
    result = await channel.send(all_alerts()["test"], 1)
    assert not result.ok and result.error == type(error).__name__
    assert len(http.calls) == 2  # русский голос, затем английский (D8)


# --- D8: английский голос ---


async def test_при_ошибке_один_повтор_английским_голосом_и_текстом(server: LocalTlsServer) -> None:
    server.route("GET", PATH, scripted((200, "Error: voice not supported"), (200, "Calling...")))
    channel = make(server)
    result = await channel.send(all_alerts()["slots_alert"], 3)
    await channel.close()
    assert result.ok and result.error == "" and result.attempt == 3
    first, second = (r.query for r in server.requests)
    assert (first["lang"], first["text"]) == (RU, CALL_TEXT_RU)
    assert (second["lang"], second["text"]) == (FALLBACK_LANG, CALL_TEXT_EN)
    assert second["user"] == first["user"] == f"@{USER}" and second["rpt"] == "2"


async def test_повтор_и_при_http_ошибке(server: LocalTlsServer) -> None:
    server.route("GET", PATH, scripted((500, "x"), (200, "ok")))
    channel = make(server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert result.ok and len(server.requests) == 2


async def test_если_и_английский_не_прошёл_ошибка_от_второй_попытки(
    server: LocalTlsServer,
) -> None:
    server.route("GET", PATH, scripted((200, "invalid voice"), (503, "x")))
    channel = make(server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and result.error == "HTTP 503"
    assert len(server.requests) == 2  # ровно один повтор, не больше


async def test_английский_язык_без_повтора(server: LocalTlsServer) -> None:
    server.respond("GET", PATH, status=500, text="x")
    channel = make(server, lang=FALLBACK_LANG)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and len(server.requests) == 1


async def test_успех_с_первой_попытки_без_повтора(server: LocalTlsServer) -> None:
    server.respond("GET", PATH, text="ok")
    channel = make(server)
    await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert len(server.requests) == 1


async def test_задержка_включает_обе_попытки(server: LocalTlsServer) -> None:
    async def slow(_: web.Request) -> web.StreamResponse:
        await asyncio.sleep(0.04)
        return web.Response(status=500, text="x")

    server.route("GET", PATH, slow)
    channel = make(server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and result.latency_ms >= 75


# --- секреты ---


async def test_имя_пользователя_не_попадает_в_логи(
    server: LocalTlsServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    server.respond("GET", PATH, status=500, text=f"Error for @{USER}")
    channel = make(server)
    result = await channel.send(all_alerts()["slots_alert"], 1)
    await channel.close()
    assert not result.ok
    # access-лог тестового сервера aiohttp печатает адрес запроса; проверяется только код бота
    ours = " | ".join(r.getMessage() for r in caplog.records if r.name.startswith("rcbot"))
    assert "CallMeBot" in ours
    assert USER not in ours
    assert USER not in result.error


# --- прогрев и закрытие ---


async def test_прогрев_head_на_главную_callmebot() -> None:
    http = RaisingHttp(aiohttp.ClientConnectionError("сети нет"))
    channel = CallMeBotNotifier(http, USER, RU)  # type: ignore[arg-type]
    with pytest.raises(aiohttp.ClientConnectionError):
        await channel.warmup()
    assert http.calls == [("HEAD", "https://api.callmebot.com/")]


async def test_прогрев_не_ходит_на_чужой_хост() -> None:
    """Без разрешения хоста запрос отклоняется до сети — настоящий сервис тесты не трогают."""
    channel = CallMeBotNotifier(SafeHttp({"127.0.0.1"}, 1), USER, RU)
    with pytest.raises(HostNotAllowed):
        await channel.warmup()
    await channel.close()


async def test_close_закрывает_сессию(server: LocalTlsServer) -> None:
    http = http_for(server)
    channel = CallMeBotNotifier(http, USER, RU)
    session = http.session
    await channel.close()
    assert session.closed
