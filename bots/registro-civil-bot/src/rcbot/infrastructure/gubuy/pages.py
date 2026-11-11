"""Распознавание страниц bpmgob по адресу и тексту (SPEC 1.3). Чистые функции, без браузера.

Признаки подобраны по записи экрана, на живом сайте не проверены: уточняются на этапе 3.
"""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlparse

LOGIN_HOST = "mi.iduruguay.gub.uy"
NO_SLOTS_TEXT = "No hay cupos disponibles"
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
    if any(marker in text for marker in STEP1_MARKERS):
        return PageKind.STEP1
    if any(marker in text for marker in AGENDA_MARKERS):
        return PageKind.AGENDA
    return PageKind.UNKNOWN
