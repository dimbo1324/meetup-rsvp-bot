"""Расчёт ключевых моментов встречи (раздел 5.2 спецификации).

Календарные вычисления («суббота, 17:00») делаются в локальном часовом поясе, результат
переводится в UTC. Функции чистые: время им передаётся снаружи.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Montevideo"


class InvalidEventTimes(ValueError):
    """Нарушен инвариант порядка моментов встречи."""


@dataclass(frozen=True)
class ScheduleSettings:
    """Настройки расписания; значения по умолчанию — из раздела 7 спецификации."""

    timezone: str = DEFAULT_TIMEZONE
    event_weekday: int = 5
    event_time: time = time(17, 0)
    cancel_deadline_days_before: int = 1
    cancel_deadline_time: time = time(20, 0)
    registration_close_hours_before: int = 0
    reminder_hours_before: int = 24
    attendance_prompt_hours_after: int = 5


@dataclass(frozen=True)
class EventTimes:
    starts_at: datetime
    cancel_deadline_at: datetime
    registration_closes_at: datetime
    reminder_at: datetime
    attendance_prompt_at: datetime


def next_event_start(now: datetime, settings: ScheduleSettings) -> datetime:
    """Ближайшее будущее начало встречи (UTC) по дню недели и времени из настроек."""
    tz = ZoneInfo(settings.timezone)
    local_now = now.astimezone(tz)
    days_ahead = (settings.event_weekday - local_now.weekday()) % 7
    candidate = datetime.combine(
        local_now.date() + timedelta(days=days_ahead), settings.event_time, tzinfo=tz
    )
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def compute_times(starts_at: datetime, settings: ScheduleSettings) -> EventTimes:
    """Вычисляет моменты встречи; дедлайн отмены не может быть позже закрытия записи."""
    if starts_at.tzinfo is None:
        raise ValueError("starts_at должен быть aware-datetime")
    tz = ZoneInfo(settings.timezone)
    starts_utc = starts_at.astimezone(UTC)
    deadline_date = starts_utc.astimezone(tz).date() - timedelta(
        days=settings.cancel_deadline_days_before
    )
    deadline = datetime.combine(deadline_date, settings.cancel_deadline_time, tzinfo=tz)
    closes = starts_utc - timedelta(hours=settings.registration_close_hours_before)
    return EventTimes(
        starts_at=starts_utc,
        cancel_deadline_at=min(deadline.astimezone(UTC), closes),
        registration_closes_at=closes,
        reminder_at=starts_utc - timedelta(hours=settings.reminder_hours_before),
        attendance_prompt_at=starts_utc + timedelta(hours=settings.attendance_prompt_hours_after),
    )


def validate_times(times: EventTimes, created_at: datetime) -> None:
    """Порядок: создание < дедлайн отмены <= закрытие записи <= начало < запрос явки."""
    ordered = (
        created_at < times.cancel_deadline_at,
        times.cancel_deadline_at <= times.registration_closes_at,
        times.registration_closes_at <= times.starts_at,
        times.starts_at < times.attendance_prompt_at,
    )
    if not all(ordered):
        raise InvalidEventTimes(
            "нарушен порядок: создание < дедлайн отмены <= закрытие записи <= начало < запрос явки"
        )
