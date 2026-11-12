"""SafeHttp на настоящих сокетах и TLS к локальному серверу (SPEC 2.2, 2.6)."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web
from tests.fakes import LocalTlsServer

from rcbot.infrastructure.http.client import SafeHttp


def _http(server: LocalTlsServer, timeout: float = 5.0) -> SafeHttp:
    return SafeHttp({"127.0.0.1"}, timeout, ssl_context=server.client_ssl)


async def test_ответ_читается_а_заголовки_уходят(tls_server: LocalTlsServer) -> None:
    tls_server.respond("GET", "/ok", text="привет")
    http = _http(tls_server)
    try:
        async with http.request("GET", tls_server.url("/ok"), headers={"X-Test": "1"}) as resp:
            assert resp.status == 200
            assert await resp.text() == "привет"
    finally:
        await http.close()
    assert tls_server.requests[0].headers["X-Test"] == "1"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_переадресация_не_выполняется_ответ_возвращается_как_есть(
    tls_server: LocalTlsServer, status: int
) -> None:
    tls_server.respond("GET", "/a", status=status, headers={"Location": "/b"})
    tls_server.respond("GET", "/b", text="цель")
    http = _http(tls_server)
    try:
        async with http.request("GET", tls_server.url("/a")) as resp:
            assert resp.status == status
            assert resp.headers["Location"] == "/b"
    finally:
        await http.close()
    assert [r.path for r in tls_server.requests] == ["/a"]


async def test_переадресация_на_чужой_хост_не_уводит_запрос(tls_server: LocalTlsServer) -> None:
    tls_server.respond("GET", "/a", status=302, headers={"Location": "https://evil.example/x"})
    http = _http(tls_server)
    try:
        async with http.request("GET", tls_server.url("/a")) as resp:
            assert resp.status == 302
    finally:
        await http.close()
    assert len(tls_server.requests) == 1


async def test_чужой_сертификат_отклоняется(tls_server: LocalTlsServer) -> None:
    # Без доверия к тестовому центру сертификации работает обычная проверка TLS
    http = SafeHttp({"127.0.0.1"}, 5)
    try:
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            async with http.request("GET", tls_server.url("/x")):
                pass
    finally:
        await http.close()
    assert tls_server.requests == []  # до запроса дело не дошло


async def test_сертификат_другого_центра_отклоняется_даже_с_контекстом() -> None:
    async with LocalTlsServer() as server, LocalTlsServer() as other:
        http = SafeHttp({"127.0.0.1"}, 5, ssl_context=other.client_ssl)  # доверяет не тому ЦС
        try:
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                async with http.request("GET", server.url("/x")):
                    pass
        finally:
            await http.close()


async def test_http_без_tls_не_принимается(tls_server: LocalTlsServer) -> None:
    from rcbot.domain.errors import HostNotAllowed

    http = _http(tls_server)
    with pytest.raises(HostNotAllowed):
        http.request("GET", tls_server.url("/x").replace("https://", "http://"))
    assert tls_server.requests == []


async def test_сессия_одна_и_переиспользуется_с_лимитом_четыре(tls_server: LocalTlsServer) -> None:
    peers: list[object] = []

    async def handler(request: web.Request) -> web.Response:
        peers.append(request.transport.get_extra_info("peername"))  # type: ignore[union-attr]
        return web.Response(text="ok")

    tls_server.route("GET", "/p", handler)
    http = _http(tls_server)
    try:
        first = http.session
        for _ in range(3):
            async with http.request("GET", tls_server.url("/p")) as resp:
                await resp.read()
        assert http.session is first
        assert first.connector is not None
        assert first.connector.limit == 4
        assert first.connector._keepalive_timeout == 75  # type: ignore[attr-defined]
    finally:
        await http.close()
    # keep-alive: все три запроса прошли по одному соединению
    assert len(set(peers)) == 1


async def test_лимит_одновременных_соединений_соблюдается(tls_server: LocalTlsServer) -> None:
    active = 0
    peak = 0

    async def handler(_: web.Request) -> web.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return web.Response(text="ok")

    tls_server.route("GET", "/slow", handler)
    http = _http(tls_server)

    async def one() -> int:
        async with http.request("GET", tls_server.url("/slow")) as resp:
            return resp.status

    try:
        assert await asyncio.gather(*(one() for _ in range(10))) == [200] * 10
    finally:
        await http.close()
    assert peak <= 4


async def test_close_идемпотентен_и_после_него_создаётся_новая_сессия(
    tls_server: LocalTlsServer,
) -> None:
    tls_server.respond("GET", "/ok", text="ok")
    http = _http(tls_server)
    await http.close()  # до первого использования: сессии ещё нет
    first = http.session
    await http.close()
    await http.close()
    assert first.closed
    second = http.session
    assert second is not first
    try:
        async with http.request("GET", tls_server.url("/ok")) as resp:
            assert resp.status == 200
    finally:
        await http.close()


async def test_общий_таймаут_запроса(tls_server: LocalTlsServer) -> None:
    async def slow(_: web.Request) -> web.Response:
        await asyncio.sleep(2)
        return web.Response(text="поздно")

    tls_server.route("GET", "/slow", slow)
    http = _http(tls_server, timeout=0.2)
    try:
        with pytest.raises(TimeoutError):
            async with http.request("GET", tls_server.url("/slow")) as resp:
                await resp.read()
    finally:
        await http.close()


async def test_свой_cookie_jar_используется_и_cookies_уходят(tls_server: LocalTlsServer) -> None:
    from yarl import URL

    tls_server.respond("GET", "/c", text="ok")
    jar = aiohttp.CookieJar(unsafe=True)  # unsafe: куки для IP-адреса
    http = SafeHttp({"127.0.0.1"}, 5, cookie_jar=jar, ssl_context=tls_server.client_ssl)
    try:
        assert http.cookie_jar is jar
        jar.update_cookies(
            {"simple_bpm_session": "TEST-ЗНАЧЕНИЕ"}, response_url=URL(tls_server.url())
        )
        async with http.request("GET", tls_server.url("/c")) as resp:
            await resp.read()
    finally:
        await http.close()
    assert "simple_bpm_session=" in tls_server.requests[0].headers["Cookie"]
