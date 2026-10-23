"""Общие исключения сервисов."""

from __future__ import annotations


class DomainError(Exception):
    """Базовая ошибка бизнес-правил; handler превращает её в понятный ответ."""


class PermissionDenied(DomainError):
    """У пользователя нет прав на действие («Нет доступа»)."""


class NotFound(DomainError):
    """Встреча, запись или пользователь не найдены."""


class InvalidOperation(DomainError):
    """Операция недопустима в текущем состоянии (например, лимит ниже занятых мест)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
