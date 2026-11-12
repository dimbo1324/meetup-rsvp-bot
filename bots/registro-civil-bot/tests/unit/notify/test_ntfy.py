"""NtfyNotifier (SPEC 2.4): тело запроса, приватность, ошибки сети, задержка, секретная тема."""

from __future__ import annotations

import asyncio
import json
import logging

import aiohttp
import pytest
from aiohttp import web

from rcbot.domain.models import Alert, AlertAction, AlertKind, Channel
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.notify.ntfy import NtfyNotifier
from tests.fakes import AGENDA_URL, LocalTlsServer
from tests.unit.notify.helpers import (
    PRIVATE_MARKERS,
    RaisingHttp,
    all_alerts,
    hang_up,
    http_for,
    stall,
)

TOPIC = "tema-secreta-0123456789abcdef0123456789abcdef"


def notifier(server: LocalTlsServer, timeout: float = 5.0) -> NtfyNotifier:
    return NtfyNotifier(http_for(server, timeout), server.url("").rstrip("/"), TOPIC)


def body_of(server: LocalTlsServer) -> dict[str, object]:
    assert len(server.requests) == 1
    payload: dict[str, object] = json.loads(server.requests[0].body)
    return payload


# --- тело запроса ---


async def test_тело_json_ровно_по_spec(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", text="{}")
    alert = all_alerts()["slots_alert"]
    channel = notifier(tls_server)
    result = await channel.send(alert, 1)
    await channel.close()

    assert result.ok and result.channel is Channel.NTFY and result.error == ""
    request = tls_server.requests[0]
    assert request.method == "POST" and request.path == "/"
    assert "application/json" in request.headers["Content-Type"]
    assert json.loads(request.body) == {
        "topic": TOPIC,
        "title": alert.push_title,
        "message": alert.push_text,
        "priority": 5,
        "tags": ["rotating_light"],
        "click": AGENDA_URL,
    }


async def test_click_только_если_задан(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", text="{}")
    alert = Alert(AlertKind.APPEARED, "текст", push_title="Заголовок", push_text="Свободно: 23.02")
    channel = notifier(tls_server)
    await channel.send(alert, 1)
    await channel.close()
    assert "click" not in body_of(tls_server)


async def test_заголовок_по_умолчанию_когда_не_задан(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", text="{}")
    channel = notifier(tls_server)
    await channel.send(Alert(AlertKind.TEST, "t", push_text="Свободно: 23.02"), 1)
    await channel.close()
    assert body_of(tls_server)["title"] == "rcbot"


async def test_повтор_уходит_с_тем_же_телом_и_номером_попытки(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", text="{}")
    alert = all_alerts()["slots_alert"]
    channel = notifier(tls_server)
    first = await channel.send(alert, 1)
    third = await channel.send(alert, 3)
    await channel.close()
    assert (first.attempt, third.attempt) == (1, 3)
    assert tls_server.requests[0].body == tls_server.requests[1].body


async def test_номер_инцидента_попадает_в_результат(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", text="{}")
    alert = Alert(AlertKind.APPEARED, "т", push_text="Свободно", incident_id=42)
    channel = notifier(tls_server)
    result = await channel.send(alert, 1)
    await channel.close()
    assert result.incident_id == 42


# --- приватность (SPEC 2.4, 2.6; инвариант 6) ---


@pytest.mark.parametrize("name", list(all_alerts()))
async def test_в_push_нет_заявки_и_ссылок_для_любого_вида_тревоги(
    tls_server: LocalTlsServer, name: str
) -> None:
    """Каждый вид тревоги, даже предназначенный только для Telegram, не выдаёт текст целиком."""
    tls_server.respond("POST", "/", text="{}")
    alert = all_alerts()[name]
    channel = notifier(tls_server)
    await channel.send(alert, 1)
    await channel.close()
    sent = body_of(tls_server)
    visible = f"{sent['title']}\n{sent['message']}"
    for marker in PRIVATE_MARKERS:
        assert marker not in visible, f"{name}: «{marker}» ушёл в ntfy"


async def test_push_содержит_даты_для_настоящей_тревоги(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", text="{}")
    channel = notifier(tls_server)
    await channel.send(all_alerts()["slots_alert"], 1)
    await channel.close()
    message = str(body_of(tls_server)["message"])
    assert "23.02" in message and "24.02" in message


async def test_без_push_text_уходит_общая_фраза_а_не_полный_текст(
    tls_server: LocalTlsServer,
) -> None:
    tls_server.respond("POST", "/", text="{}")
    alert = Alert(AlertKind.SERVICE, f"Заявка {PRIVATE_MARKERS[0]} → {AGENDA_URL}")
    channel = notifier(tls_server)
    await channel.send(alert, 1)
    await channel.close()
    message = str(body_of(tls_server)["message"])
    assert message.strip()
    assert PRIVATE_MARKERS[0] not in message and "bpmgob" not in message


# --- ошибки ---


@pytest.mark.parametrize("status", [201, 400, 403, 429, 500, 503])
async def test_не_200_это_ошибка_с_кодом(tls_server: LocalTlsServer, status: int) -> None:
    tls_server.respond("POST", "/", status=status, text="x")
    channel = notifier(tls_server)
    result = await channel.send(all_alerts()["test"], 2)
    await channel.close()
    assert not result.ok
    assert result.error == f"HTTP {status}"
    assert result.attempt == 2


async def test_переадресация_не_считается_успехом(tls_server: LocalTlsServer) -> None:
    """302 не выполняется (SafeHttp не ходит по редиректам) и считается ошибкой."""
    tls_server.respond("POST", "/", status=302, headers={"Location": "https://evil.example/"})
    channel = notifier(tls_server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and result.error == "HTTP 302"
    assert len(tls_server.requests) == 1


async def test_таймаут_сервера_это_ошибка_без_исключения(tls_server: LocalTlsServer) -> None:
    tls_server.route("POST", "/", stall)
    channel = notifier(tls_server, timeout=0.15)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok and result.error == "TimeoutError"
    assert 100 <= result.latency_ms < 2000


async def test_обрыв_соединения_это_ошибка_без_исключения(tls_server: LocalTlsServer) -> None:
    tls_server.route("POST", "/", hang_up)
    channel = notifier(tls_server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert not result.ok
    assert result.error and result.error != "HTTP 200"


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (aiohttp.ClientConnectionError("отказ"), "ClientConnectionError"),
        (aiohttp.ServerDisconnectedError(), "ServerDisconnectedError"),
        (aiohttp.ClientPayloadError("обрыв"), "ClientPayloadError"),
        (TimeoutError(), "TimeoutError"),
    ],
)
async def test_любая_сетевая_ошибка_это_ошибка_результата(error: BaseException, name: str) -> None:
    http = RaisingHttp(error)
    channel = NtfyNotifier(http, "https://ntfy.sh", TOPIC)  # type: ignore[arg-type]
    result = await channel.send(all_alerts()["test"], 1)
    assert not result.ok and result.error == name
    assert http.calls == [("POST", "https://ntfy.sh/")]


async def test_не_сетевое_исключение_не_маскируется() -> None:
    """Ошибка программирования не должна выдавать себя за сбой сети."""
    channel = NtfyNotifier(RaisingHttp(ValueError("баг")), "https://ntfy.sh", TOPIC)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await channel.send(all_alerts()["test"], 1)


async def test_недоверенный_сертификат_отклоняется() -> None:
    """Проверка TLS не отключается: сервер с чужим центром сертификации — ошибка, не отправка."""
    async with LocalTlsServer() as server, LocalTlsServer() as stranger:
        server.respond("POST", "/", text="{}")
        http = SafeHttp({"127.0.0.1"}, 3, ssl_context=stranger.client_ssl)
        channel = NtfyNotifier(http, server.url("").rstrip("/"), TOPIC)
        result = await channel.send(all_alerts()["test"], 1)
        await channel.close()
    assert not result.ok
    assert result.error == "ClientConnectorCertificateError"


# --- задержка ---


async def test_задержка_измеряется_по_времени_ответа(tls_server: LocalTlsServer) -> None:
    async def slow(_: web.Request) -> web.StreamResponse:
        await asyncio.sleep(0.05)
        return web.Response(text="{}")

    tls_server.route("POST", "/", slow)
    channel = notifier(tls_server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert result.ok
    assert 45 <= result.latency_ms < 2000


# --- секрет темы не в логах ---


async def test_тема_не_попадает_в_логи_при_сбоях(
    tls_server: LocalTlsServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    tls_server.respond("POST", "/", status=500, text=f"ошибка для {TOPIC}")
    channel = notifier(tls_server)
    await channel.send(all_alerts()["slots_alert"], 1)
    await channel.close()
    tls_server.route("POST", "/", hang_up)
    channel = notifier(tls_server)
    await channel.send(all_alerts()["slots_alert"], 1)
    await channel.close()
    assert "ntfy" in caplog.text  # сбой записан в журнал, значит, проверка не пустая
    assert TOPIC not in caplog.text
    assert "TEST-TRAMITE" not in caplog.text


async def test_ошибка_в_результате_не_содержит_темы(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", status=500, text=TOPIC)
    channel = notifier(tls_server)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert TOPIC not in result.error


# --- прогрев, закрытие ---


async def test_прогрев_идёт_на_health_без_темы(tls_server: LocalTlsServer) -> None:
    tls_server.respond("GET", "/v1/health", text='{"healthy":true}')
    channel = notifier(tls_server)
    await channel.warmup()
    await channel.close()
    request = tls_server.requests[0]
    assert (request.method, request.path) == ("GET", "/v1/health")
    assert TOPIC not in request.path and not request.query and not request.body


async def test_прогрев_соединения_ускоряет_первую_отправку_через_то_же_подключение(
    tls_server: LocalTlsServer,
) -> None:
    """Прогрев и отправка идут через одну сессию: соединение переиспользуется (SPEC 2.2)."""
    tls_server.respond("GET", "/v1/health", text="ok")
    tls_server.respond("POST", "/", text="{}")
    http = http_for(tls_server)
    channel = NtfyNotifier(http, tls_server.url("").rstrip("/"), TOPIC)
    await channel.warmup()
    session = http.session
    await channel.send(all_alerts()["test"], 1)
    assert http.session is session
    await channel.close()
    assert session.closed


async def test_close_закрывает_сессию_и_повторный_close_безопасен(
    tls_server: LocalTlsServer,
) -> None:
    http = http_for(tls_server)
    channel = NtfyNotifier(http, tls_server.url("").rstrip("/"), TOPIC)
    session = http.session
    await channel.close()
    await channel.close()
    assert session.closed


async def test_канал_это_ntfy() -> None:
    assert NtfyNotifier(SafeHttp({"ntfy.sh"}, 1), "https://ntfy.sh/", TOPIC).channel is Channel.NTFY


async def test_адрес_сервера_с_косой_чертой_нормализуется(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", text="{}")
    channel = NtfyNotifier(http_for(tls_server), tls_server.url("/"), TOPIC)
    result = await channel.send(all_alerts()["test"], 1)
    await channel.close()
    assert result.ok and tls_server.requests[0].path == "/"


async def test_чужой_хост_отклоняется_до_сети() -> None:
    """Список разрешённых хостов (SPEC 2.6): запрос к постороннему адресу не уходит."""
    http = SafeHttp({"ntfy.sh"}, 1)
    channel = NtfyNotifier(http, "https://127.0.0.1:9", TOPIC)
    from rcbot.domain.errors import HostNotAllowed

    with pytest.raises(HostNotAllowed):
        await channel.send(all_alerts()["test"], 1)
    await channel.close()


async def test_действие_ack_не_меняет_тело(tls_server: LocalTlsServer) -> None:
    tls_server.respond("POST", "/", text="{}")
    alert = Alert(
        AlertKind.APPEARED, "т", push_text="Свободно", action=AlertAction.ACK, incident_id=1
    )
    channel = notifier(tls_server)
    await channel.send(alert, 1)
    await channel.close()
    assert set(body_of(tls_server)) == {"topic", "title", "message", "priority", "tags"}
