"""Разведка (DiscoveryRecorder) и проверка доступа (probe_agenda) в настоящем Chromium.

Сайт — локальный сервер (site.py); владельца, который входит руками, заменяет страница,
сама уходящая на bpmgob через 0,3 с.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout
from tests.integration.gubuy.site import (
    AGENDA,
    BPM_HOST,
    HOME_PAGE,
    ID_HOST,
    LOGIN_BPM,
    LOGIN_ID,
    LOGIN_ID_RETURN,
    OFFICE,
    FakeSite,
    MonthSpec,
    agenda_page,
    green,
    grey,
    routed_launch,
    rows,
)

from rcbot.application.ports import SessionCookie
from rcbot.infrastructure.gubuy import browser, discovery
from rcbot.infrastructure.gubuy.browser import BrowserOptions
from rcbot.infrastructure.gubuy.discovery import DiscoveryRecorder
from rcbot.infrastructure.gubuy.pages import PageKind
from rcbot.infrastructure.gubuy.probe import probe_agenda
from rcbot.infrastructure.logging.redaction import REDACTOR

pytestmark = pytest.mark.browser

AT = datetime(2026, 11, 12, 15, 4, 5, tzinfo=UTC)
NOV = MonthSpec("Noviembre 2026", rows(grey(1), green(5)))
DIC = MonthSpec("Diciembre 2026", rows(green(3)))
ENE = MonthSpec("Enero 2027", rows(green(7)))
API = f"https://{BPM_HOST}/agenda_sae_api_disponibilidades"
SCRIPT_URL = f"https://{BPM_HOST}/js/calendario-sae.js"

XHR_SCRIPT = """
const call = (url, opts) => fetch(url, opts).then(r => r.text());
call('/agenda_sae_api_disponibilidades?fecha=2026-11&nombre=AnaPrueba&x=1',
     {headers: {'X-Requested-With': 'XMLHttpRequest', 'X-Debug': 'token-visible'}});
call('/otro/xhr?documento=TEST000000', {method: 'POST', body: 'apellido=Prueba&fecha=1'});
call('/socket.io/?EIO=3&transport=polling');
const s = document.createElement('script'); s.src = '/js/calendario-sae.js?_=1';
document.head.appendChild(s);
"""


@pytest.fixture
async def site(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeSite]:
    async with FakeSite() as fake:
        monkeypatch.setattr(browser, "launch", routed_launch(fake))
        monkeypatch.setattr(discovery, "launch", routed_launch(fake))
        yield fake
        for opened in fake.browsers:
            assert not opened.is_connected(), "браузер остался открытым"


def serve_login_and_agenda(site: FakeSite) -> None:
    # Вход «владельца»: страница ID, которая сама возвращает на bpmgob; в её адресе есть
    # и адрес bpmgob (redirect_uri) — это ещё не возврат
    site.redirect(LOGIN_BPM, LOGIN_ID_RETURN)
    site.page(
        LOGIN_ID_RETURN,
        f"<html><script>setTimeout(() => location.href = '{HOME_PAGE}', 300)</script></html>",
    )
    site.page(
        HOME_PAGE,
        "<html>Inicio Ana Prueba</html>",
        **{"Set-Cookie": "simple_bpm_session=TEST-SESSION; Path=/; Secure; HttpOnly"},
    )
    site.page(AGENDA, agenda_page([NOV, DIC, ENE], extra_script=XHR_SCRIPT))
    site.handlers[(BPM_HOST, "/agenda_sae_api_disponibilidades")] = lambda _: (
        200,
        json.dumps(
            {"nombre": "Ana Prueba", "mail": "ana.prueba@example.org", "dias": [5, 12], "ok": 1}
        ),
        {"Content-Type": "application/json", "Set-Cookie": "api_cookie=API-COOKIE; Path=/"},
    )
    site.handlers[(BPM_HOST, "/js/calendario-sae.js")] = lambda _: (
        200,
        "var API = '/agenda_sae_api_disponibilidades'; // correo ana.prueba@example.org",
        {"Content-Type": "application/javascript"},
    )
    site.handlers[(BPM_HOST, "/otro/xhr")] = lambda _: (200, "otro", {"Content-Type": "text/plain"})
    site.handlers[(BPM_HOST, "/socket.io/")] = lambda _: (200, "ok", {"Content-Type": "text/plain"})


def recorder(tmp_path: Path, months: int = 3) -> DiscoveryRecorder:
    return DiscoveryRecorder(
        LOGIN_BPM, AGENDA, OFFICE, months, tmp_path / "discovery", BrowserOptions(headless=True)
    )


# --- разведка ---


async def test_сквозной_прогон_записывает_только_xhr_и_скрипт_календаря(
    site: FakeSite, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    serve_login_and_agenda(site)
    result = await recorder(tmp_path).run(AT, wait_login_sec=15)

    assert result.path == tmp_path / "discovery" / "discovery-20261112T150405.json"
    urls = [r["url"] for r in result.records]
    assert any("agenda_sae_api_disponibilidades" in u for u in urls)
    assert any("calendario-sae.js" in u for u in urls)
    assert any("socket.io" in u for u in urls)
    # документы, стили и страницы ID Uruguay не записываются
    assert not any(
        u.rstrip("/").endswith(("/etapas/ejecutar/TEST-ETAPA/1", "/etapas/home")) for u in urls
    )
    assert all(ID_HOST not in u for u in urls)
    assert {r["resource"] for r in result.records} <= {"xhr", "fetch", "script"}
    assert "Войдите в аккаунт" in capsys.readouterr().out


async def test_разведка_листает_месяцы_только_стрелкой(site: FakeSite, tmp_path: Path) -> None:
    serve_login_and_agenda(site)
    await recorder(tmp_path, months=3).run(AT, wait_login_sec=15)
    assert site.non_get() != []  # POST из страницы-макета — её собственный XHR, не действие бота
    assert [r.path for r in site.non_get()] == ["/otro/xhr"]
    assert not [r for r in site.requests if "guardar" in r.url]  # форма не отправлялась


async def test_в_дамп_не_попадают_cookies_заголовки_и_личные_данные(
    site: FakeSite, tmp_path: Path
) -> None:
    REDACTOR.register("TEST-SESSION")
    serve_login_and_agenda(site)
    result = await recorder(tmp_path).run(AT, wait_login_sec=15)
    text = result.path.read_text(encoding="utf-8")

    for forbidden in (
        "Ana Prueba",
        "AnaPrueba",
        "ana.prueba@example.org",
        "TEST000000",
        "TEST-SESSION",
        "API-COOKIE",
        "Titular",  # текст страницы с данными владельца
        "Paso 2 de 3",  # страницы целиком не пишутся
    ):
        assert forbidden not in text, forbidden
    for record in result.records:
        for headers in (record["request_headers"], record["response_headers"]):
            assert not {"cookie", "set-cookie", "authorization"} & {k.lower() for k in headers}

    api = next(r for r in result.records if "agenda_sae_api_disponibilidades" in r["url"])
    assert api["status"] == 200
    assert api["method"] == "GET"
    assert api["body"]["nombre"] == "***"
    assert api["body"]["mail"] == "***"
    assert api["body"]["dias"] == [5, 12]  # календарные поля остаются
    assert "nombre=***" in api["url"]
    assert "fecha=2026-11" in api["url"]
    assert api["request_headers"]["x-requested-with"] == "XMLHttpRequest"  # нужен для ApiSource
    post = next(r for r in result.records if r["method"] == "POST")
    assert "apellido=***" in post["post_data"]
    assert "documento=***" in post["url"]


async def test_файл_дампа_читается_как_json_и_сводка_готова(site: FakeSite, tmp_path: Path) -> None:
    serve_login_and_agenda(site)
    result = await recorder(tmp_path).run(AT, wait_login_sec=15)
    saved = json.loads(result.path.read_text(encoding="utf-8"))
    assert saved["recorded_at"] == AT.isoformat()
    assert len(saved["records"]) == len(result.records) >= 3
    lines = result.summary()
    assert any("<== API календаря" in line for line in lines)
    assert any("socket.io" in line and "Замечен" in line for line in lines)


async def test_владелец_не_вошёл_это_таймаут_без_файла_и_браузер_закрыт(
    site: FakeSite, tmp_path: Path
) -> None:
    site.page(LOGIN_BPM, "<html>Ingrese</html>")  # на страницу входа никто не заходит
    with pytest.raises(PlaywrightTimeout):
        await recorder(tmp_path).run(AT, wait_login_sec=1)
    assert not (tmp_path / "discovery").exists()


async def test_страница_agenda_без_радиокнопки_разведку_не_ломает(
    site: FakeSite, tmp_path: Path
) -> None:
    # Страница «Agenda» без радиокнопки: разведка продолжает и записывает то, что есть
    serve_login_and_agenda(site)
    site.page(AGENDA, agenda_page([NOV], show_calendar_without_radio=True, radio=False))
    result = await recorder(tmp_path, months=1).run(AT, wait_login_sec=15)
    assert result.path.exists()


# --- probe_agenda ---


@pytest.mark.parametrize(
    ("serve", "expected"),
    [
        ("agenda", PageKind.AGENDA),
        ("login", PageKind.LOGIN),
        ("step1", PageKind.STEP1),
        ("blocked", PageKind.BLOCKED),
        ("block_text", PageKind.BLOCKED),
        ("unknown", PageKind.UNKNOWN),
    ],
)
async def test_probe_различает_страницы_и_шлёт_cookies(
    site: FakeSite, serve: str, expected: PageKind
) -> None:
    if serve == "agenda":
        site.page(AGENDA, agenda_page([NOV]))
    elif serve == "login":
        site.redirect(AGENDA, LOGIN_ID)
        site.page(LOGIN_ID, "<html>Elige un método para identificarte</html>")
    elif serve == "step1":
        site.page(AGENDA, "<html><b>Verificación de seguridad</b></html>")
    elif serve == "blocked":
        site.page(AGENDA, "<html>Hola</html>", status=403)
    elif serve == "block_text":
        site.page(AGENDA, "<html>Request Rejected</html>")
    else:
        site.page(AGENDA, "<html>Hola</html>")
    cookies = [SessionCookie("simple_bpm_session", "TEST-VALUE", BPM_HOST)]
    kind = await probe_agenda(AGENDA, cookies, BrowserOptions(headless=True))
    assert kind is expected
    assert "simple_bpm_session=TEST-VALUE" in site.requests[0].header("cookie")
    assert site.non_get() == []
    assert all(not b.is_connected() for b in site.browsers)
