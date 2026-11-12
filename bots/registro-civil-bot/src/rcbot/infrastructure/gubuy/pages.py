"""Распознавание страниц bpmgob по адресу и тексту (SPEC 1.3). Чистые функции, без браузера.

Признаки подобраны по записи экрана, на живом сайте не проверены: уточняются на этапе 3.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

LOGIN_HOST = "mi.iduruguay.gub.uy"
BPMGOB_HOST = "bpmgob.mec.gub.uy"
NO_SLOTS_TEXT = "No hay cupos disponibles"
# Вместо ответа API может прийти HTML входа: адрес у него — адрес API, и по адресу страницу
# входа не узнать (SPEC 2.3: «HTML вместо ожидаемого ответа» — признак истёкшей сессии)
LOGIN_MARKERS = (
    "Elige un método para identificarte",
    "Elige un metodo para identificarte",
)
STEP1_MARKERS = (
    "Verificación de seguridad",
    "Verificacion de seguridad",
    "Solicitar otra pregunta",
)
AGENDA_MARKERS = ("Seleccionar día", "Seleccionar dia", "ui-datepicker", "Paso 2 de 3")
# Страница отказа защитного шлюза F5 (ASM) и типовые тексты блокировки
BLOCK_MARKERS = ("The requested URL was rejected", "Request Rejected", "Access Denied")


class PageKind(StrEnum):
    LOGIN = "login"
    STEP1 = "step1"
    AGENDA = "agenda"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


def classify(url: str, text: str, status: int | None = None) -> PageKind:
    parsed = urlparse(url)
    if status in (403, 429) or any(marker in text for marker in BLOCK_MARKERS):
        return PageKind.BLOCKED
    if (parsed.hostname or "") == LOGIN_HOST or parsed.path.startswith("/autenticacion"):
        return PageKind.LOGIN
    if any(marker in text for marker in LOGIN_MARKERS):
        return PageKind.LOGIN
    if any(marker in text for marker in STEP1_MARKERS):
        return PageKind.STEP1
    if any(marker in text for marker in AGENDA_MARKERS):
        return PageKind.AGENDA
    return PageKind.UNKNOWN


def is_login_url(url: str) -> bool:
    """Мы на странице ID Uruguay; хост сравнивается строго, не подстрокой адреса."""
    return (urlparse(url).hostname or "") == LOGIN_HOST


def is_bpmgob_session_url(url: str) -> bool:
    """Мы на bpmgob вне страницы входа, то есть вход завершён.

    Хост сравнивается строго: адрес bpmgob в запросе страницы ID Uruguay (`redirect_uri=…`) или
    чужой поддомен возвратом не считаются.
    """
    parsed = urlparse(url)
    return (parsed.hostname or "") == BPMGOB_HOST and not parsed.path.startswith("/autenticacion")
