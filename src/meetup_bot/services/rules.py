"""Настройки правил, которые читают сервисы (значения по умолчанию — раздел 7).

Сервисы получают этот объект (или его часть) параметром; собирает его из БД сервис настроек.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class RegistrationOptions:
    """Включаемые настройки правил записи (раздел 7): лист ожидания включён, «+1» выключен."""

    waitlist_enabled: bool = True
    plus_one_enabled: bool = False


@dataclass(frozen=True)
class RulesSettings:
    ban_after_no_shows: int = 1
    remove_banned_from_future_events: bool = True
    notify_user_on_ban: bool = False
    late_promotion_hours: int = 24
    notify_admins_on_full: bool = True
    notify_admins_on_cancel: bool = True
    reminder_enabled: bool = True
    show_names_in_post: bool = False
    auto_create_enabled: bool = True
    auto_create_days_after: int = 1
    auto_create_time: time = time(10, 0)
    post_reminder_days_before: int = 2
    post_reminder_time: time = time(10, 0)
