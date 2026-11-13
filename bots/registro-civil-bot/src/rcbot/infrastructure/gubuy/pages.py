"""Распознавание страниц bpmgob по адресу и тексту (SPEC 1.3). Чистые функции, без браузера.

Признаки подобраны по записи экрана, на живом сайте не проверены: уточняются на этапе 3.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlparse

from rcbot.config.settings import BPMGOB_HOST
from rcbot.domain.errors import ManualStepRequired, RateLimited, RcbotError, SessionExpired

__all__ = [
    "AGENDA_MARKERS",
    "BLOCK_MARKERS",
    "BPMGOB_HOST",
    "LOGIN_HOST",
    "LOGIN_MARKERS",
    "LOGIN_PATH_PREFIX",
    "NO_SLOTS_TEXT",
    "PAGE_ERRORS",
    "RATE_LIMIT_STATUSES",
    "STEP1_MARKERS",
    "PageKind",
    "classify",
    "is_bpmgob_session_url",
    "is_login_redirect",
    "is_login_url",
]

LOGIN_HOST = "mi.iduruguay.gub.uy"
# Страница входа самого bpmgob: с неё сайт уводит на ID Uruguay
LOGIN_PATH_PREFIX = "/autenticacion"
# Статусы отказа защитного шлюза: шаг назад по лестнице частоты (D4)
RATE_LIMIT_STATUSES = frozenset({403, 429})
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


# Страницы, которые означают не «сломанный ответ», а известное состояние сайта. Текст исключения
# пишет вызывающий: источник знает, что именно он ждал вместо такой страницы
PAGE_ERRORS: Mapping[PageKind, type[RcbotError]] = MappingProxyType(
    {
        PageKind.LOGIN: SessionExpired,
        PageKind.STEP1: ManualStepRequired,
        PageKind.BLOCKED: RateLimited,
    }
)


def classify(url: str, text: str, status: int | None = None) -> PageKind:
    parsed = urlparse(url)
    if status in RATE_LIMIT_STATUSES or any(marker in text for marker in BLOCK_MARKERS):
        return PageKind.BLOCKED
    if (parsed.hostname or "") == LOGIN_HOST or parsed.path.startswith(LOGIN_PATH_PREFIX):
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


def is_login_redirect(location: str) -> bool:
    """Переадресация на вход: страница ID Uruguay или `/autenticacion` самого bpmgob.

    Разбираем адрес, а не ищем подстроку: чужой адрес с «mi.iduruguay.gub.uy» в запросе — не вход.
    Относительный адрес (пустой хост) считается адресом bpmgob.
    """
    parsed = urlparse(location)
    host = (parsed.hostname or "").lower()
    if host == LOGIN_HOST:
        return True
    return host in ("", BPMGOB_HOST) and parsed.path.startswith(LOGIN_PATH_PREFIX)


def is_bpmgob_session_url(url: str) -> bool:
    """Мы на bpmgob вне страницы входа, то есть вход завершён.

    Хост сравнивается строго: адрес bpmgob в запросе страницы ID Uruguay (`redirect_uri=…`) или
    чужой поддомен возвратом не считаются.
    """
    parsed = urlparse(url)
    return (parsed.hostname or "") == BPMGOB_HOST and not parsed.path.startswith(LOGIN_PATH_PREFIX)
