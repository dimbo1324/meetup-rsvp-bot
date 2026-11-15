"""Исключения SPEC 3.1. Текст исключения не должен содержать секретов и личных данных."""

from __future__ import annotations


class RcbotError(Exception):
    """Базовая ошибка бота."""


class RateLimited(RcbotError):
    """403/429 или страница блокировки: шаг назад по лестнице частоты и долгая пауза (D4)."""


class UnexpectedResponse(RcbotError):
    """Ответ не соответствует ожидаемому: не страница записи, переадресация, не тот статус."""


class SiteUnavailable(RcbotError):
    """Сеть, таймаут или 5xx: экспоненциальная пауза."""


class HostNotAllowed(RcbotError):
    """Запрос к хосту вне списка разрешённых (SPEC 2.6)."""
