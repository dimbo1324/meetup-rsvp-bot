"""Сеть только по списку хостов и только с проверкой TLS (SPEC 2.6, инвариант 8).

Динамические проверки идут на настоящем сокете к `LocalTlsServer`; статические читают исходники
`src/rcbot`: граница, описанная только комментарием, не граница.
"""

from __future__ import annotations

import ast
import re
import time
from collections.abc import Iterator
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from yarl import URL

import rcbot
from rcbot.config.settings import Settings
from rcbot.domain.errors import HostNotAllowed
from rcbot.infrastructure.http.client import SafeHttp
from tests.fakes import LocalTlsServer

SRC = Path(rcbot.__file__).resolve().parent
# SPEC 2.6: «bpmgob.mec.gub.uy, mi.iduruguay.gub.uy, api.telegram.org, ntfy.sh, api.callmebot.com,
# адрес "сторожа" (если включён)»
SPEC_HOSTS = {
    "bpmgob.mec.gub.uy",
    "mi.iduruguay.gub.uy",
    "api.telegram.org",
    "ntfy.sh",
    "api.callmebot.com",
}


def py_files() -> Iterator[Path]:
    yield from sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


# --- список хостов ---


def test_список_разрешённых_хостов_по_умолчанию_равен_списку_spec() -> None:
    assert Settings().allowed_hosts() == frozenset(SPEC_HOSTS)


def test_сторож_добавляет_ровно_свой_хост_а_sae_до_реализации_ничего(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RCBOT_WATCHDOG_URL", "https://hc-ping.example/abc-uuid")
    assert Settings().allowed_hosts() == frozenset(SPEC_HOSTS | {"hc-ping.example"})
    # Решение R11: чтение SAE не реализовано, флаг сеть не расширяет
    monkeypatch.setenv("RCBOT_SAE_SOURCE_ENABLED", "true")
    assert Settings().allowed_hosts() == frozenset(SPEC_HOSTS | {"hc-ping.example"})


def test_http_в_настройках_отклоняется() -> None:
    for name in ("RCBOT_WATCHDOG_URL", "RCBOT_NTFY_SERVER", "RCBOT_API_URL", "RCBOT_AGENDA_URL"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv(name, "http://example.org/x")
            with pytest.raises(ValueError, match="https"):
                Settings()


def test_api_url_на_чужом_хосте_отклоняется_и_список_не_расширяется(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for url in (
        "https://evil.example/agenda_sae_api_disponibilidades",
        "https://bpmgob.mec.gub.uy.evil.example/x",
        "https://sub.bpmgob.mec.gub.uy/x",
    ):
        monkeypatch.setenv("RCBOT_API_URL", url)
        with pytest.raises(ValueError, match=r"bpmgob\.mec\.gub\.uy"):
            Settings()
    monkeypatch.setenv(
        "RCBOT_API_URL", "https://bpmgob.mec.gub.uy/api/agenda_sae_api_disponibilidades"
    )
    assert Settings().allowed_hosts() == frozenset(SPEC_HOSTS)


# --- проверка адреса ---


def test_битый_адрес_даёт_hostnotallowed_а_не_valueerror() -> None:
    with pytest.raises(HostNotAllowed):
        SafeHttp({"ntfy.sh"}, 5).check("https://[ntfy.sh/")


http = SafeHttp({"bpmgob.mec.gub.uy", "ntfy.sh"}, 5)

DENIED = [
    "https://bpmgob.mec.gub.uy.evil.example/",
    "https://evilbpmgob.mec.gub.uy/",
    "https://evil-bpmgob.mec.gub.uy.example/",
    "https://sub.bpmgob.mec.gub.uy/",
    "https://mec.gub.uy/",
    "https://gub.uy/",
    "https://evil.example/bpmgob.mec.gub.uy",
    "https://evil.example/?h=bpmgob.mec.gub.uy",
    "https://evil.example#@bpmgob.mec.gub.uy/",
    "https://evil.example?@bpmgob.mec.gub.uy/",
    "https://bpmgob.mec.gub.uy@evil.example/",
    "https://bpmgob.mec.gub.uy:443@evil.example/",
    "https://bpmgob.mec.gub.uy:pw@evil.example/",
    "https://bpmgob.mec.gub.uy./",
    "https://bpmgоb.mec.gub.uy/",
    "https://ｂpmgob.mec.gub.uy/",
    "https://xn--bpmgb-xxx.mec.gub.uy/",
    "https://bpmgob.mec.gub.uy%2eevil.example/",
    "https://bpmgob.mec.gub.uy%00.evil.example/",
    "https://127.0.0.1/",
    "https://[::1]/",
    "https://2130706433/",
    "https://localhost/",
    "http://bpmgob.mec.gub.uy/",
    "ftp://bpmgob.mec.gub.uy/",
    "ws://bpmgob.mec.gub.uy/",
    "wss://bpmgob.mec.gub.uy/",
    "file:///C:/Windows/win.ini",
    "//bpmgob.mec.gub.uy/",
    "https:bpmgob.mec.gub.uy",
    "bpmgob.mec.gub.uy",
    "",
    "https://",
]


@pytest.mark.parametrize("url", DENIED)
def test_адрес_вне_списка_или_не_https_отклоняется(url: str) -> None:
    with pytest.raises(HostNotAllowed):
        http.check(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1",
        "https://BPMGOB.MEC.GUB.UY/x",
        "HTTPS://bpmgob.mec.gub.uy/x",
        "https://bpmgob.mec.gub.uy:443/x",
        "https://ntfy.sh/",
        "https://ntfy.sh/v1/health",
    ],
)
def test_допустимые_адреса_пропускаются(url: str) -> None:
    """Проверка не должна отказывать законному: регистр и порт 443 — тот же хост."""
    http.check(url)


CLIENT_BREAKERS = ["\\", "#", "?", "@", "%2f", "\t", "\n", " ", "%00", "[", "]", ":", "."]


def test_проверка_и_клиент_aiohttp_видят_один_и_тот_же_хост() -> None:
    """Адрес, который прошёл `check`, должен вести на разрешённый хост и для yarl (его
    использует aiohttp), иначе разбор в двух библиотеках расходится и открывает обход."""
    allowed = {"bpmgob.mec.gub.uy", "ntfy.sh"}
    hosts = ["bpmgob.mec.gub.uy", "evil.example"]
    checked = 0
    for a in hosts:
        for b in hosts:
            for sep in CLIENT_BREAKERS:
                for tail in ("", "/x"):
                    for url in (
                        f"https://{a}{sep}{b}{tail}",
                        f"https://{a}{sep}@{b}{tail}",
                        f"https://{a}@{b}{sep}{tail}",
                    ):
                        try:
                            http.check(url)
                        except HostNotAllowed, ValueError:
                            continue
                        checked += 1
                        try:
                            host = URL(url).host
                        except ValueError:
                            continue  # клиент сам откажется: безопасно
                        assert host in allowed, (
                            f"{url!r}: check пропустил, а yarl ведёт на {host!r}"
                        )
    assert checked > 10  # цикл не пустой: допустимое тоже встречается


async def test_переадресация_не_выполняется_и_остаётся_ответом(tls_server: LocalTlsServer) -> None:
    tls_server.respond("GET", "/go", status=302, headers={"Location": "https://evil.example/steal"})
    client = SafeHttp({"127.0.0.1"}, 5, ssl_context=tls_server.client_ssl)
    try:
        async with client.request("GET", tls_server.url("/go")) as response:
            assert response.status == 302
            assert response.headers["Location"] == "https://evil.example/steal"
    finally:
        await client.close()
    assert [r.path for r in tls_server.requests] == ["/go"]


async def test_запрос_к_запрещённому_хосту_не_открывает_соединения(
    tls_server: LocalTlsServer,
) -> None:
    client = SafeHttp({"127.0.0.1"}, 5, ssl_context=tls_server.client_ssl)
    try:
        with pytest.raises(HostNotAllowed):
            client.request("GET", f"https://localhost:{tls_server.port}/x")
        assert client._session is None  # отказ раньше создания сессии
    finally:
        await client.close()
    assert tls_server.requests == []


# --- TLS проверяется всегда ---


async def test_контроль_без_доверия_к_центру_сертификации_запрос_отклоняется(
    tls_server: LocalTlsServer,
) -> None:
    """Базовая гарантия: сертификат тестового CA системе неизвестен, значит, проверка работает."""
    tls_server.respond("GET", "/x", text="ok")
    client = SafeHttp({"127.0.0.1"}, 5)  # системные сертификаты, как в работе
    try:
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            async with client.request("GET", tls_server.url("/x")):
                pass
    finally:
        await client.close()
    assert tls_server.requests == []


async def test_с_доверенным_центром_запрос_проходит(tls_server: LocalTlsServer) -> None:
    tls_server.respond("GET", "/x", text="ok")
    client = SafeHttp({"127.0.0.1"}, 5, ssl_context=tls_server.client_ssl)
    try:
        async with client.request("GET", tls_server.url("/x")) as response:
            assert await response.text() == "ok"
    finally:
        await client.close()


@pytest.mark.parametrize("override", [False, None])
async def test_вызов_с_ssl_false_отклоняется_и_сети_не_касается(
    tls_server: LocalTlsServer, override: bool | None
) -> None:
    tls_server.respond("GET", "/x", text="ok")
    client = SafeHttp({"127.0.0.1"}, 5)
    try:
        with pytest.raises(TypeError, match="ssl"):
            async with client.request("GET", tls_server.url("/x"), ssl=override):
                pass
    finally:
        await client.close()
    assert tls_server.requests == []


# --- статика: нигде в src нет отключения TLS, прокси и посторонних клиентов ---

TLS_OFF = [
    (re.compile(r"\bssl\s*=\s*(False|None)\b"), "ssl=False"),
    (re.compile(r"\bverify(_ssl)?\s*=\s*False\b"), "verify=False"),
    (re.compile(r"CERT_NONE"), "ssl.CERT_NONE"),
    (re.compile(r"check_hostname\s*=\s*False"), "check_hostname = False"),
    (re.compile(r"_create_unverified_context|_create_default_https_context"), "unverified context"),
    (re.compile(r"ignore_https_errors|ignoreHTTPSErrors"), "ignore_https_errors (Playwright)"),
    (re.compile(r"--ignore-certificate-errors|--allow-insecure-localhost"), "флаг Chromium"),
    (re.compile(r"PYTHONHTTPSVERIFY|TLS_REJECT_UNAUTHORIZED"), "переменная окружения"),
    (re.compile(r"\bFingerprint\("), "aiohttp.Fingerprint (обход цепочки доверия)"),
]


def _code_lines(path: Path) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.split("#", 1)[0]
        if stripped.strip():
            yield number, stripped


def test_в_исходниках_нет_отключения_проверки_tls() -> None:
    found = [
        f"{path.relative_to(SRC)}:{number}: {label}"
        for path in py_files()
        for number, line in _code_lines(path)
        for pattern, label in TLS_OFF
        if pattern.search(line)
    ]
    assert found == []


def test_статический_контроль_шаблоны_tls_ловят_плохой_код() -> None:
    bad = [
        "session.get(url, ssl=False)",
        "requests.get(url, verify=False)",
        "ctx.verify_mode = ssl.CERT_NONE",
        "ctx.check_hostname = False",
        "browser.new_context(ignore_https_errors=True)",
        "args=['--ignore-certificate-errors']",
    ]
    for line in bad:
        assert any(p.search(line) for p, _ in TLS_OFF), line
    assert not any(p.search("ssl_context=ctx, ssl=ctx") for p, _ in TLS_OFF[:2])


def test_clientsession_создаётся_только_в_safehttp() -> None:
    owners = set()
    for path in py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in {"ClientSession", "TCPConnector"}:
                    owners.add(str(path.relative_to(SRC)).replace("\\", "/"))
    assert owners == {"infrastructure/http/client.py"}


FORBIDDEN_CLIENTS = {"requests", "httpx", "urllib3", "urllib.request", "http.client", "websockets"}


def test_посторонних_http_клиентов_нет() -> None:
    found = []
    for path in py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            found += [f"{path.relative_to(SRC)}: {n}" for n in names if n in FORBIDDEN_CLIENTS]
    assert found == []


def test_нет_прокси_и_переменных_окружения_прокси() -> None:
    """SPEC 0.3, п. 2: один IP, без прокси."""
    pattern = re.compile(r"\bproxy\w*\s*=|trust_env\s*=\s*True|--proxy-server|HTTPS?_PROXY", re.I)
    found = [
        f"{path.relative_to(SRC)}:{number}"
        for path in py_files()
        for number, line in _code_lines(path)
        if pattern.search(line)
    ]
    assert found == []


def test_ни_один_вызов_не_следует_за_переадресациями_сам() -> None:
    """`SafeHttp` проверяет адрес только запроса, не цели перехода: переходить сам нельзя."""
    found = [
        f"{path.relative_to(SRC)}:{number}"
        for path in py_files()
        for number, line in _code_lines(path)
        if re.search(r"allow_redirects\s*=\s*True|max_redirects\s*=", line)
    ]
    assert found == []


# --- таймауты ---


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def test_каждый_safehttp_создаётся_с_положительным_таймаутом() -> None:
    found = 0
    for path in py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == "SafeHttp":
                found += 1
                keywords = {k.arg: k.value for k in node.keywords}
                value = keywords.get("timeout_sec") or (
                    node.args[1] if len(node.args) > 1 else None
                )
                assert value is not None, f"{path.name}:{node.lineno}: SafeHttp без таймаута"
                assert not (isinstance(value, ast.Constant) and not value.value), (
                    f"{path.name}:{node.lineno}: нулевой таймаут"
                )
    assert found >= 4


def test_в_исходниках_нет_отключения_таймаута() -> None:
    found = [
        f"{path.relative_to(SRC)}:{number}"
        for path in py_files()
        for number, line in _code_lines(path)
        if re.search(r"total\s*=\s*None|timeout\s*=\s*None|ClientTimeout\(\s*\)", line)
    ]
    assert found == []


async def test_запрос_к_зависшему_серверу_обрывается_по_таймауту(
    tls_server: LocalTlsServer,
) -> None:
    import asyncio

    async def hang(_: web.Request) -> web.StreamResponse:
        await asyncio.sleep(3)
        return web.Response(text="late")

    tls_server.route("GET", "/hang", hang)
    client = SafeHttp({"127.0.0.1"}, 0.2, ssl_context=tls_server.client_ssl)
    started = time.perf_counter()
    try:
        with pytest.raises(TimeoutError):
            async with client.request("GET", tls_server.url("/hang")) as response:
                await response.read()
    finally:
        await client.close()
    assert time.perf_counter() - started < 2.0


def test_сессия_получает_таймаут_из_конструктора() -> None:
    client = SafeHttp({"ntfy.sh"}, 7.5)
    assert client._timeout.total == 7.5


def test_telegram_сессия_aiogram_имеет_конечный_таймаут() -> None:
    from aiogram import Bot

    bot = Bot("123456789" + ":" + "A" * 35)
    timeout = bot.session.timeout
    assert 0 < float(timeout if isinstance(timeout, int | float) else timeout.total or 0) <= 120
