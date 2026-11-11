"""Исключения SPEC 3.1. Текст исключения не должен содержать секретов и личных данных."""

from __future__ import annotations


class RcbotError(Exception):
    """Базовая ошибка бота."""


class SessionExpired(RcbotError):
    """Сайт вернул страницу входа вместо календаря."""


class ManualStepRequired(RcbotError):
    """Сайт требует шаг 1 с контрольным вопросом: проходит владелец (D3)."""


class RateLimited(RcbotError):
    """403/429 или страница блокировки: шаг назад по лестнице частоты и долгая пауза (D4)."""


class UnexpectedResponse(RcbotError):
    """Ответ не соответствует ожидаемой модели: снимок уходит в карантин."""


class SiteUnavailable(RcbotError):
    """Сеть, таймаут или 5xx: экспоненциальная пауза."""


class ReloginBudgetExceeded(RcbotError):
    """Больше разрешённого числа входов в час."""


class SourceNotConfigured(RcbotError):
    """Источник нельзя использовать: нет нужной настройки (например, адреса API до разведки)."""


class HostNotAllowed(RcbotError):
    """Запрос к хосту вне списка разрешённых (SPEC 2.6)."""
