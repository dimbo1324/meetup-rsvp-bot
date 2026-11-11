"""Настройки правил, которые читают сервисы (значения по умолчанию — `services/defaults.py`).

Сервисы получают этот объект (или его часть) параметром; собирает его из БД сервис настроек.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from meetup_bot.services.defaults import (
    DEFAULT_AUTO_CREATE_DAYS_AFTER,
    DEFAULT_AUTO_CREATE_ENABLED,
    DEFAULT_AUTO_CREATE_TIME,
    DEFAULT_BAN_AFTER_NO_SHOWS,
    DEFAULT_LATE_PROMOTION_HOURS,
    DEFAULT_NOTIFY_ADMINS_ON_CANCEL,
    DEFAULT_NOTIFY_ADMINS_ON_FULL,
    DEFAULT_NOTIFY_USER_ON_BAN,
    DEFAULT_PLUS_ONE_ENABLED,
    DEFAULT_POST_REMINDER_DAYS_BEFORE,
    DEFAULT_POST_REMINDER_TIME,
    DEFAULT_REMINDER_ENABLED,
    DEFAULT_REMOVE_BANNED_FROM_FUTURE_EVENTS,
    DEFAULT_SHOW_NAMES_IN_POST,
    DEFAULT_WAITLIST_ENABLED,
)


@dataclass(frozen=True)
class RegistrationOptions:
    """Включаемые настройки правил записи (раздел 7)."""

    waitlist_enabled: bool = DEFAULT_WAITLIST_ENABLED
    plus_one_enabled: bool = DEFAULT_PLUS_ONE_ENABLED


@dataclass(frozen=True)
class RulesSettings:
    ban_after_no_shows: int = DEFAULT_BAN_AFTER_NO_SHOWS
    remove_banned_from_future_events: bool = DEFAULT_REMOVE_BANNED_FROM_FUTURE_EVENTS
    notify_user_on_ban: bool = DEFAULT_NOTIFY_USER_ON_BAN
    late_promotion_hours: int = DEFAULT_LATE_PROMOTION_HOURS
    notify_admins_on_full: bool = DEFAULT_NOTIFY_ADMINS_ON_FULL
    notify_admins_on_cancel: bool = DEFAULT_NOTIFY_ADMINS_ON_CANCEL
    reminder_enabled: bool = DEFAULT_REMINDER_ENABLED
    show_names_in_post: bool = DEFAULT_SHOW_NAMES_IN_POST
    auto_create_enabled: bool = DEFAULT_AUTO_CREATE_ENABLED
    auto_create_days_after: int = DEFAULT_AUTO_CREATE_DAYS_AFTER
    auto_create_time: time = DEFAULT_AUTO_CREATE_TIME
    post_reminder_days_before: int = DEFAULT_POST_REMINDER_DAYS_BEFORE
    post_reminder_time: time = DEFAULT_POST_REMINDER_TIME
