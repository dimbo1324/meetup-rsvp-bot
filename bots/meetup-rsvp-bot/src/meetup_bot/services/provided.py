"""Провайдеры настроек в конструкторах сервисов: значение, функция или ничего (умолчания)."""

from __future__ import annotations

from collections.abc import Callable

from meetup_bot.services.admin_support import Provided, resolve


def resolve_or_default[T](value: Provided[T] | None, default: Callable[[], T]) -> T:
    """Текущее значение настройки; без провайдера — умолчания (тесты, утилиты)."""
    return default() if value is None else resolve(value)
