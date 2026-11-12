"""Распознавание страниц bpmgob (SPEC 1.3): каждая ветка `classify` и порядок приоритетов."""

from __future__ import annotations

import pytest

from rcbot.infrastructure.gubuy.pages import PageKind, classify

AGENDA_URL = "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1"
LOGIN_URL = "https://mi.iduruguay.gub.uy/login?process_state=TEST"
AUTH_URL = "https://bpmgob.mec.gub.uy/autenticacion/login?redirect=x"


@pytest.mark.parametrize(
    "url",
    [
        LOGIN_URL,
        "https://MI.IDURUGUAY.GUB.UY/sending-saml-response",
        "https://mi.iduruguay.gub.uy:8443/login",
        AUTH_URL,
        "https://bpmgob.mec.gub.uy/autenticacion",
    ],
)
def test_страница_входа_узнаётся_по_адресу(url: str) -> None:
    assert classify(url, "<html>что угодно</html>", 200) is PageKind.LOGIN


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.mi.iduruguay.gub.uy/login",
        "https://mi.iduruguay.gub.uy.evil.com/login",
        "https://bpmgob.mec.gub.uy/otra/autenticacion",
        "https://bpmgob.mec.gub.uy/?next=/autenticacion",
    ],
)
def test_похожий_адрес_не_считается_входом(url: str) -> None:
    assert classify(url, "пусто", 200) is PageKind.UNKNOWN


@pytest.mark.parametrize(
    "marker",
    [
        "Verificación de seguridad",
        "Verificacion de seguridad",
        "Solicitar otra pregunta",
    ],
)
def test_шаг_один_узнаётся_по_каждому_признаку(marker: str) -> None:
    assert classify(AGENDA_URL, f"<div>{marker}</div>", 200) is PageKind.STEP1


@pytest.mark.parametrize(
    "marker",
    ["Seleccionar día", "Seleccionar dia", "ui-datepicker", "Paso 2 de 3"],
)
def test_шаг_agenda_узнаётся_по_каждому_признаку(marker: str) -> None:
    assert classify(AGENDA_URL, f"<div>{marker}</div>", 200) is PageKind.AGENDA


@pytest.mark.parametrize(
    "text",
    [
        "The requested URL was rejected. Please consult with your administrator.",
        "<h1>Request Rejected</h1>",
        "Access Denied",
    ],
)
def test_страница_блокировки_узнаётся_по_тексту(text: str) -> None:
    assert classify(AGENDA_URL, text, 200) is PageKind.BLOCKED


@pytest.mark.parametrize("status", [403, 429])
def test_статусы_403_и_429_это_блокировка_даже_без_текста(status: int) -> None:
    assert classify(AGENDA_URL, "обычная страница", status) is PageKind.BLOCKED


@pytest.mark.parametrize("status", [None, 200, 301, 302, 400, 401, 404, 500, 502, 503])
def test_прочие_статусы_сами_по_себе_не_блокировка(status: int | None) -> None:
    assert classify(AGENDA_URL, "обычная страница", status) is PageKind.UNKNOWN


@pytest.mark.parametrize("text", ["", "   ", "<html><body>Hola</body></html>", "NO HAY AGENDA"])
def test_страница_без_признаков_неизвестна(text: str) -> None:
    assert classify(AGENDA_URL, text, 200) is PageKind.UNKNOWN


def test_признаки_чувствительны_к_регистру_как_у_сайта() -> None:
    # Тексты сайта не меняют регистр: «verificación de seguridad» маленькими — не шаг 1
    assert classify(AGENDA_URL, "verificación de seguridad", 200) is PageKind.UNKNOWN


# --- приоритеты: блокировка > вход > шаг 1 > Agenda ---


@pytest.mark.parametrize("url", [LOGIN_URL, AUTH_URL, AGENDA_URL])
def test_блокировка_приоритетнее_всего_остального(url: str) -> None:
    text = "Request Rejected Verificación de seguridad ui-datepicker"
    assert classify(url, text, 200) is PageKind.BLOCKED
    assert classify(url, "ui-datepicker", 429) is PageKind.BLOCKED


def test_вход_приоритетнее_шага_один_и_agenda() -> None:
    assert classify(LOGIN_URL, "Verificación de seguridad ui-datepicker", 200) is PageKind.LOGIN
    assert classify(AUTH_URL, "ui-datepicker", 200) is PageKind.LOGIN


def test_шаг_один_приоритетнее_agenda() -> None:
    text = "ui-datepicker Paso 2 de 3 Solicitar otra pregunta"
    assert classify(AGENDA_URL, text, 200) is PageKind.STEP1


def test_страница_входа_по_тексту_узнаётся_и_на_адресе_api() -> None:
    # Вместо JSON API может прийти HTML входа: адрес у него — адрес API, а не страницы входа
    text = "<h1>Elige un método para identificarte en ID Uruguay</h1> Usuario Gub.uy"
    assert classify("https://bpmgob.mec.gub.uy/api/disponibilidades", text, 200) is PageKind.LOGIN
