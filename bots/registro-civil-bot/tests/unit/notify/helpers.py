"""Общие помощники тестов каналов: настоящий локальный TLS-сервер и набор всех видов тревог."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web

from rcbot.application.messages import MessageComposer
from rcbot.domain.models import Alert
from rcbot.infrastructure.http.client import SafeHttp
from tests.fakes import AGENDA_URL, T0, LocalTlsServer, slots, snapshot

# Условные «личные» значения: ни одно из них не должно уйти в ntfy и звонок (SPEC 2.4, 2.6)
APPLICATION_NUMBER = "TEST-TRAMITE-777"
STAGE_NUMBER = "TEST-ETAPA"
PRIVATE_MARKERS = (APPLICATION_NUMBER, STAGE_NUMBER, "bpmgob", "mec.gub.uy", "Заявка", "http")

TZ = ZoneInfo("America/Montevideo")


def composer() -> MessageComposer:
    return MessageComposer(TZ, AGENDA_URL, APPLICATION_NUMBER)


def all_alerts() -> dict[str, Alert]:
    """Все виды тревог, которые бот способен собрать (application/messages.py)."""
    c = composer()
    snap = snapshot(slots(23, 24))
    return {
        "slots_alert": c.slots_alert(snap, T0 + timedelta(seconds=1)),
        "slots_changed": c.slots_alert(snap, T0 + timedelta(seconds=1), added=slots(24)),
        "unconfirmed": c.unconfirmed_alert(snap),
        "gone": c.gone(T0),
        "manual_step": c.manual_step("контрольный вопрос"),
        "service": c.service("🔀 Источник данных: api → dom. Причина: тест."),
        "test": c.test(T0),
    }


def http_for(server: LocalTlsServer, timeout: float = 5.0) -> SafeHttp:
    return SafeHttp({"127.0.0.1"}, timeout, ssl_context=server.client_ssl)


async def stall(_: web.Request) -> web.StreamResponse:
    """Сервер принял запрос и молчит: проверка настоящего таймаута клиента."""
    await asyncio.sleep(0.4)
    return web.Response(text="поздно")


async def hang_up(request: web.Request) -> web.StreamResponse:
    """Обрыв соединения посреди запроса."""
    transport = request.transport
    assert transport is not None
    transport.abort()
    await asyncio.sleep(0)
    return web.Response(text="не дойдёт")


class RaisingHttp:
    """Подмена `SafeHttp`: любой запрос мгновенно падает заданным исключением (без сети и пауз)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.closed = 0

    def request(self, method: str, url: str, **_: Any) -> Any:
        self.calls.append((method, url))
        raise self.error

    async def close(self) -> None:
        self.closed += 1


class _Response:
    async def read(self) -> bytes:
        return b""

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class OkHttp:
    """Подмена `SafeHttp`: запрос «успешен» и записывается; сети нет."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **_: Any) -> Any:
        self.calls.append((method, url))
        return _Response()

    async def close(self) -> None:
        return None
