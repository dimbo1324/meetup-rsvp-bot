"""SafeHttp: только https и только хосты из списка (SPEC 2.6). Проверка адреса без сети."""

from __future__ import annotations

import aiohttp
import pytest

from rcbot.domain.errors import HostNotAllowed
from rcbot.infrastructure.http.client import SafeHttp

ALLOWED = ("bpmgob.mec.gub.uy", "mi.iduruguay.gub.uy")


@pytest.fixture
def http() -> SafeHttp:
    return SafeHttp(ALLOWED, 5)


@pytest.mark.parametrize(
    "url",
    [
        "https://bpmgob.mec.gub.uy/",
        "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1?x=1#f",
        "https://mi.iduruguay.gub.uy/login",
        "https://BPMGOB.MEC.GUB.UY/",  # регистр имени хоста не значим
        "https://bpmgob.mec.gub.uy:443/",
        "https://bpmgob.mec.gub.uy:8443/",  # порт хоста не меняет сам хост (его же использует тест)
    ],
)
def test_разрешённые_адреса_проходят(http: SafeHttp, url: str) -> None:
    http.check(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://bpmgob.mec.gub.uy/",  # не https
        "ftp://bpmgob.mec.gub.uy/",
        "//bpmgob.mec.gub.uy/",
        "bpmgob.mec.gub.uy/",
        "file:///etc/passwd",
        "",
        "https://",
        "https:///path",
        "https://evil.com/",
        "https://evil.bpmgob.mec.gub.uy/",  # поддомен
        "https://bpmgob.mec.gub.uy.evil.com/",  # чужой домен с нашим префиксом
        "https://xbpmgob.mec.gub.uy/",
        "https://mec.gub.uy/",
        "https://gub.uy/",
        "https://bpmgob.mec.gub.uy@evil.com/",  # userinfo: настоящий хост — после @
        "https://bpmgob.mec.gub.uy:pass@evil.com/",
        "https://bpmgob.mec.gub.uy./",  # точка в конце — другая строка, не из списка
        "https://bpmgob.mec.gub.uy..",
        "https://bpmgob.mec.gub.uy%2eevil.com/",
        "https://evil.com#@bpmgob.mec.gub.uy/",
        "https://evil.com?@bpmgob.mec.gub.uy/",
        "https://evil.com/@bpmgob.mec.gub.uy/",
        "https://bpmgоb.mec.gub.uy/",  # кириллическая «о» вместо латинской
        "https://bpmgob.mec.gub.uy/".replace("mec", "mес"),  # кириллическая «с»
        "https://xn--bpmgob-5dh.mec.gub.uy/",  # punycode-подделка
        "https://ｂｐｍｇｏｂ.mec.gub.uy/",  # полноширинные буквы
        "https://bpmgob。mec。gub。uy/",  # идеографические точки
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://localhost/",
    ],
)
def test_всё_остальное_отклоняется(http: SafeHttp, url: str) -> None:
    with pytest.raises(HostNotAllowed):
        http.check(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com\\@bpmgob.mec.gub.uy/",
        "https://evil.com\\.bpmgob.mec.gub.uy/",
        "https://bpmgob.mec.gub.uy\\@evil.com/",
    ],
)
def test_разный_разбор_адреса_urllib_и_aiohttp_не_обходит_список(http: SafeHttp, url: str) -> None:
    # urllib видит здесь хост из списка, а aiohttp (yarl) — другой или отказывает. Адрес, который
    # две библиотеки читают по-разному, отклоняется ещё до сети
    with pytest.raises(HostNotAllowed):
        http.check(url)


def test_список_хостов_без_учёта_регистра_в_настройках() -> None:
    SafeHttp(["BPMGOB.mec.gub.uy"], 5).check("https://bpmgob.mec.gub.uy/")


def test_пустой_список_отклоняет_всё() -> None:
    with pytest.raises(HostNotAllowed):
        SafeHttp([], 5).check("https://bpmgob.mec.gub.uy/")


def test_сообщение_об_отказе_не_содержит_адрес_целиком_с_секретами(http: SafeHttp) -> None:
    # В адресе может быть токен в запросе: в исключение идёт только схема и хост
    with pytest.raises(HostNotAllowed) as info:
        http.check("https://evil.com/path?token=ЗНАЧЕНИЕ-ТОКЕНА")
    assert "ЗНАЧЕНИЕ-ТОКЕНА" not in str(info.value)
    assert "evil.com" in str(info.value)


@pytest.mark.parametrize(
    "option",
    [
        {"ssl": False},
        {"ssl": None},
        {"verify_ssl": False},
        {"ssl_context": object()},
        {"fingerprint": b"x" * 32},
        {"allow_redirects": True},
        {"max_redirects": 10},
        {"proxy": "http://127.0.0.1:3128"},
        {"proxy_auth": aiohttp.BasicAuth("u", "p")},
        {"trust_env": True},
    ],
)
async def test_вызывающий_не_может_ослабить_защиту_через_kwargs(
    http: SafeHttp, option: dict[str, object]
) -> None:
    # SPEC 2.6: TLS проверяется всегда; SPEC 0.3: без прокси. Переадресации не выполняются,
    # иначе мимо списка хостов можно уйти ответом 302
    try:
        with pytest.raises(TypeError, match="не разрешён"):
            http.request("GET", "https://bpmgob.mec.gub.uy/", **option)
    finally:
        await http.close()


async def test_запрос_с_запрещённым_хостом_не_создаёт_сессию() -> None:
    http = SafeHttp(ALLOWED, 5)
    with pytest.raises(HostNotAllowed):
        http.request("GET", "https://evil.com/")
    assert http._session is None  # проверка идёт до любой сети


async def test_cookies_переживают_закрытие_и_новую_сессию(tls_server) -> None:
    # Находка T9 №4: после reset_source клиент создавал сессию с пустым хранилищем cookies
    # и бот терял вход
    from rcbot.infrastructure.http.client import SafeHttp

    http = SafeHttp({"127.0.0.1"}, 5, ssl_context=tls_server.client_ssl)
    try:
        jar_before = http.cookie_jar
        await http.close()
        jar_after = http.cookie_jar  # новая сессия
        assert jar_after is jar_before
    finally:
        await http.close()


def test_предел_соединений_константа_а_не_параметр_конструктора() -> None:
    import inspect

    from rcbot.infrastructure.http import client

    assert client.CONNECTOR_LIMIT == 4
    assert "limit" not in inspect.signature(client.SafeHttp).parameters
