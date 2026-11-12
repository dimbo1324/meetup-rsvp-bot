"""HttpWatchdog (SPEC 6.1): пустой запрос «сторожу» без каких-либо данных."""

from __future__ import annotations

import aiohttp
import pytest

from rcbot.domain.errors import HostNotAllowed
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.notify.watchdog import HttpWatchdog
from tests.fakes import LocalTlsServer
from tests.unit.notify.helpers import RaisingHttp, hang_up, http_for, stall

PING = "/ping/00000000-test"


def watchdog(server: LocalTlsServer, timeout: float = 5.0) -> HttpWatchdog:
    return HttpWatchdog(http_for(server, timeout), server.url(PING))


@pytest.mark.parametrize("status", [200, 201, 204])
async def test_успешный_пинг(tls_server: LocalTlsServer, status: int) -> None:
    tls_server.respond("GET", PING, status=status, text="OK")
    dog = watchdog(tls_server)
    assert await dog.ping() is True
    await dog.close()


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_ошибка_сервиса_это_неудача(tls_server: LocalTlsServer, status: int) -> None:
    tls_server.respond("GET", PING, status=status, text="x")
    dog = watchdog(tls_server)
    assert await dog.ping() is False
    await dog.close()


async def test_пинг_пустой_без_данных(tls_server: LocalTlsServer) -> None:
    """В запросе нет тела, параметров, cookies и авторизации (SPEC 6.1, инвариант 6)."""
    tls_server.respond("GET", PING, text="OK")
    dog = watchdog(tls_server)
    await dog.ping()
    await dog.close()
    request = tls_server.requests[0]
    assert (request.method, request.path) == ("GET", PING)
    assert request.body == b"" and request.query == {}
    lowered = {name.lower() for name in request.headers}
    assert not lowered & {"cookie", "authorization", "x-api-key"}


async def test_таймаут_это_неудача_без_исключения(tls_server: LocalTlsServer) -> None:
    tls_server.route("GET", PING, stall)
    dog = watchdog(tls_server, timeout=0.15)
    assert await dog.ping() is False
    await dog.close()


async def test_обрыв_это_неудача_без_исключения(tls_server: LocalTlsServer) -> None:
    tls_server.route("GET", PING, hang_up)
    dog = watchdog(tls_server)
    assert await dog.ping() is False
    await dog.close()


@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError("x"), TimeoutError()])
async def test_сетевые_ошибки_не_пробрасываются(error: BaseException) -> None:
    dog = HttpWatchdog(RaisingHttp(error), "https://hc-ping.com/x")  # type: ignore[arg-type]
    assert await dog.ping() is False


async def test_чужой_хост_отклоняется_до_сети() -> None:
    dog = HttpWatchdog(SafeHttp({"hc-ping.com"}, 1), "https://127.0.0.1:9/x")
    with pytest.raises(HostNotAllowed):
        await dog.ping()
    await dog.close()


async def test_close_закрывает_сессию(tls_server: LocalTlsServer) -> None:
    http = http_for(tls_server)
    session = http.session
    await HttpWatchdog(http, tls_server.url(PING)).close()
    assert session.closed
