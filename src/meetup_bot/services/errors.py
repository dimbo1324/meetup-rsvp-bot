"""Общие исключения сервисов."""

from __future__ import annotations

import enum


class ErrorCode(enum.StrEnum):
    """Коды `InvalidOperation`: по ним UI выбирает понятный текст, а не по сообщению."""

    ALREADY_ADMIN = "already_admin"
    ALREADY_REGISTERED = "already_registered"
    ATTENDANCE_WINDOW_CLOSED = "attendance_window_closed"
    BAD_STATUS = "bad_status"
    CANNOT_BAN_ADMIN = "cannot_ban_admin"
    CANNOT_REMOVE_OWNER = "cannot_remove_owner"
    CAPACITY_BELOW_TAKEN = "capacity_below_taken"
    EVENT_CANCELLED = "event_cancelled"
    EVENT_NOT_STARTED = "event_not_started"
    INVALID_ATTENDANCE = "invalid_attendance"
    INVALID_CAPACITY = "invalid_capacity"
    INVALID_DESCRIPTION_HTML = "invalid_description_html"
    INVALID_GUESTS = "invalid_guests"
    INVALID_PHOTO_FILE_ID = "invalid_photo_file_id"
    INVALID_PLACE = "invalid_place"
    INVALID_PLACE_URL = "invalid_place_url"
    INVALID_TIMES = "invalid_times"
    INVALID_USER = "invalid_user"
    NO_SHOW_REQUIRES_ACTIVE = "no_show_requires_active"
    NOT_LIVE = "not_live"
    NOT_MARKED = "not_marked"
    OVER_CAPACITY = "over_capacity"
    PLUS_ONE_DISABLED = "plus_one_disabled"
    REGISTRATION_TIME_PASSED = "registration_time_passed"
    UNKNOWN_REGISTRATION = "unknown_registration"
    WHO_REQUIRED = "who_required"


class DomainError(Exception):
    """Базовая ошибка бизнес-правил; handler превращает её в понятный ответ."""


class PermissionDenied(DomainError):
    """У пользователя нет прав на действие («Нет доступа»)."""


class NotFound(DomainError):
    """Встреча, запись или пользователь не найдены."""


class InvalidOperation(DomainError):
    """Операция недопустима в текущем состоянии (например, лимит ниже занятых мест)."""

    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
