"""Остальные пути утечки: Sentry, разведка, карантин, исключения наружу, cookies между каналами."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiohttp import web

from rcbot.application.ports import SessionCookie
from rcbot.bootstrap import build_core
from rcbot.cli import main
from rcbot.domain.errors import HostNotAllowed, UnexpectedResponse
from rcbot.infrastructure import sentry
from rcbot.infrastructure.gubuy.api_source import ApiSource
from rcbot.infrastructure.gubuy.browser import session_cookies
from rcbot.infrastructure.gubuy.discovery import _clean_headers, clean_body
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR
from rcbot.runner import _notifiers
from tests.fakes import TARGET, FakeClock, LocalTlsServer
from tests.privacy.helpers import Canaries
from tests.privacy.scenario import prime_redactor

C = Canaries()
UNICODE_SECRET = "пароль-Ünï-9Zq"


@pytest.fixture
def primed() -> None:
    from rcbot.config.secrets import SecretStore

    store = SecretStore()
    for key, value in C.secrets().items():
        store.set(key, value)
    prime_redactor()
    REDACTOR.register(UNICODE_SECRET)


# --- Sentry (SPEC 2.6) ---


def _event() -> dict[str, Any]:
    return {
        "message": f"сбой входа для {C.doc_number}, пароль {C.password}",
        "logentry": {"message": "вход %s", "params": [C.telegram_token]},
        "extra": {"doc": C.doc_number, "url": f"/etapas/ejecutar/{C.etapa}/1", "u": UNICODE_SECRET},
        "exception": {
            "values": [{"type": "LoginFailed", "value": f"{C.telegram_token} {UNICODE_SECRET}"}]
        },
        "breadcrumbs": {
            "values": [
                {
                    "message": f"Cookie: simple_bpm_session={C.cookie_value}",
                    "data": {"x": C.ntfy_topic},
                }
            ]
        },
        "request": {
            "url": f"https://api.telegram.org/bot{C.telegram_token}/getMe",
            "headers": {"Cookie": "a=b; c=d", "Authorization": "Bearer q1w2e3r4", "Host": "x"},
        },
        "user": {"username": C.call_user},
    }


def test_sentry_before_send_вырезает_секреты_из_всех_частей_события(primed: None) -> None:
    result = sentry._before_send(_event(), {})
    assert result is not None
    text = json.dumps(result, ensure_ascii=False)
    assert [v for v in (*C.registered(), UNICODE_SECRET, "q1w2e3r4", "a=b; c=d") if v in text] == []
    escaped = json.dumps(result)  # то, что реально уйдёт по сети: ASCII-экранирование
    assert [v for v in (*C.registered(), "q1w2e3r4") if v in escaped] == []
    assert result["exception"]["values"][0]["type"] == "LoginFailed"  # диагностика цела
    assert result["request"]["headers"]["Host"] == "x"


def test_sentry_выключен_по_умолчанию_и_не_инициализируется_без_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    assert sentry.init_sentry(False, "https://k@o.example/1", "r") is False
    assert sentry.init_sentry(True, "", "r") is False
    assert calls == []


def test_sentry_включённый_получает_настройки_приватности(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    assert sentry.init_sentry(True, "https://k@o.example/1", "rcbot@test") is True
    (kwargs,) = calls
    assert kwargs["send_default_pii"] is False
    assert kwargs["include_local_variables"] is False
    assert kwargs["max_request_body_size"] == "never"
    assert kwargs["before_send"] is sentry._before_send
    assert kwargs["traces_sample_rate"] == 0.0


def test_sentry_событие_которое_нельзя_сериализовать_отбрасывается_а_не_уходит_сырым() -> None:
    class Weird:
        def __repr__(self) -> str:
            raise TypeError("нельзя")

    assert sentry._before_send({"extra": {"x": Weird()}}, {}) is None


# --- разведка (SPEC 6.2) ---


def test_разведка_очищает_тела_и_заголовки_от_известных_секретов(primed: None) -> None:
    body = json.dumps(
        {
            "usuario": {"nombre": "Иван", "documento": C.doc_number},
            "note": f"Cookie: simple_bpm_session={C.cookie_value} и {C.password}",
            "dias": [1, 2, 3],
        }
    )
    cleaned = json.dumps(clean_body("application/json", body), ensure_ascii=False)
    assert [v for v in C.all_values() if v in cleaned] == []
    assert "Иван" not in cleaned
    assert '"dias": [1, 2, 3]' in cleaned
    html = clean_body("text/html", f"<p>{C.doc_number} {C.telegram_token}</p>")
    assert C.doc_number not in html
    assert C.telegram_token not in html


def test_разведка_выбрасывает_cookie_и_authorization_из_заголовков(primed: None) -> None:
    headers = {
        "Cookie": "a=b",
        "SET-COOKIE": "c=d",
        "Authorization": "Bearer q",
        "Proxy-Authorization": "x",
        "Referer": f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{C.etapa}/1",
        "Accept": "*/*",
    }
    cleaned = _clean_headers(headers)
    assert set(cleaned) == {"Referer", "Accept"}
    assert C.etapa not in cleaned["Referer"]
    assert MASK in cleaned["Referer"]


def test_разведка_маскирует_csrf_и_токены_в_json_теле() -> None:
    body = '{"csrf_token": "aB3dE5fG7hJ9kL1m", "api_key": "zY8xW6vU4tS2rQ0p", "dias": [1]}'
    cleaned = json.dumps(clean_body("application/json", body))
    assert "aB3dE5fG7hJ9kL1m" not in cleaned
    assert "zY8xW6vU4tS2rQ0p" not in cleaned


def test_разведка_отбрасывает_csrf_заголовки() -> None:
    cleaned = _clean_headers({"X-CSRF-Token": "aB3dE5fG7hJ9kL1m", "X-API-Key": "zY8xW6vU4tS2rQ0p"})
    assert "aB3dE5fG7hJ9kL1m" not in json.dumps(cleaned)
    assert "zY8xW6vU4tS2rQ0p" not in json.dumps(cleaned)


# --- ApiSource и карантин на настоящем сокете ---


async def _fetch_hostile(
    server: LocalTlsServer, tmp_path: Path, *, headers: dict[str, str] | None = None
) -> tuple[UnexpectedResponse, Path]:
    body = (
        f"<html>Set-Cookie: simple_bpm_session={C.cookie_value}\n"
        f"{C.doc_number} {C.password} token={C.telegram_token}</html>"
    )

    async def handler(_: web.Request) -> web.StreamResponse:
        return web.Response(text=body, headers=headers or {"Content-Type": "text/html"})

    server.route("GET", "/api", handler)
    http = SafeHttp({"127.0.0.1"}, 5, ssl_context=server.client_ssl)
    quarantine = tmp_path / "q"
    api = ApiSource(
        http, server.url("/api"), "https://bpmgob.mec.gub.uy/x", quarantine, FakeClock()
    )
    try:
        with pytest.raises(UnexpectedResponse) as info:
            await api.fetch(TARGET)
    finally:
        await api.close()
    return info.value, quarantine


async def test_исключение_и_трассировка_источника_не_содержат_тела_ответа(
    primed: None, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    error, _ = await _fetch_hostile(tls_server, tmp_path)
    text = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert [v for v in C.all_values() if v in text] == []
    assert "карантин" in str(error)


async def test_карантин_хранит_очищенное_тело_без_заголовков(
    primed: None, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    _, quarantine = await _fetch_hostile(tls_server, tmp_path)
    (file,) = quarantine.glob("*.json")
    record = json.loads(file.read_text(encoding="utf-8"))
    assert [v for v in C.all_values() if v in json.dumps(record, ensure_ascii=False)] == []
    assert "Error" not in record["body"] or MASK in record["body"]
    assert MASK in record["body"]  # маска на месте: тело сохранено для разбора, секреты нет


async def test_карантин_не_пишет_секрет_из_content_type(
    primed: None, tls_server: LocalTlsServer, tmp_path: Path
) -> None:
    _, quarantine = await _fetch_hostile(
        tls_server, tmp_path, headers={"Content-Type": f"text/html; note={C.password}"}
    )
    (file,) = quarantine.glob("*.json")
    assert C.password not in file.read_text(encoding="utf-8")


# --- cookies: регистрация и изоляция ---


class _FakeContext:
    def __init__(self, cookies: list[dict[str, Any]]) -> None:
        self._cookies = cookies

    async def cookies(self, urls: list[str]) -> list[dict[str, Any]]:
        return self._cookies


async def test_значения_cookies_из_браузера_регистрируются_в_фильтре() -> None:
    context = _FakeContext(
        [{"name": "simple_bpm_session", "value": C.cookie_value, "domain": "bpmgob.mec.gub.uy"}]
    )
    cookies = await session_cookies(context, ["bpmgob.mec.gub.uy"])  # type: ignore[arg-type]
    assert cookies[0].value == C.cookie_value
    assert REDACTOR.redact(f'{{"sid": "{C.cookie_value}"}}') == f'{{"sid": "{MASK}"}}'


async def test_cookies_сайта_не_утекают_в_сессии_каналов_ntfy_и_callmebot(
    paths: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCBOT_AGENDA_URL", f"https://bpmgob.mec.gub.uy/etapas/ejecutar/{C.etapa}/1")
    core = build_core(console=False, command="run")
    for key, value in C.secrets().items():
        core.secrets.set(key, value)
    bot = Bot(C.telegram_token)
    notifiers = _notifiers(core, bot, int(C.chat_id))
    from rcbot.bootstrap import build_api_source

    api = build_api_source(core)
    api.use_cookies([SessionCookie("simple_bpm_session", C.cookie_value, "bpmgob.mec.gub.uy")])
    jars = [n._http.cookie_jar for n in notifiers.values() if hasattr(n, "_http")]  # type: ignore[attr-defined]
    assert len(jars) == 2
    assert api._http.cookie_jar not in jars
    assert jars[0] is not jars[1]
    assert all(len(list(jar)) == 0 for jar in jars)
    core.close()


# --- исключения наружу ---


def test_отказ_safehttp_не_содержит_пароля_из_адреса() -> None:
    http = SafeHttp({"bpmgob.mec.gub.uy"}, 5)
    with pytest.raises(HostNotAllowed) as info:
        http.check(f"https://user:SuperSecretPw1@evil.example/?token={C.telegram_token}")
    text = str(info.value)
    assert "SuperSecretPw1" not in text
    assert C.telegram_token not in text
    assert "evil.example" in text


def test_ошибка_настроек_в_cli_не_печатает_введённое_значение(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RCBOT_AGENDA_URL", f"http://bpmgob.mec.gub.uy/etapas/ejecutar/{C.etapa}/1")
    assert main(["doctor", "--offline"]) == 2
    captured = capsys.readouterr()
    assert C.etapa not in captured.err + captured.out
    assert "agenda_url" in captured.err
