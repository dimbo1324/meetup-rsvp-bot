"""Расчёт ключевых моментов встречи (раздел 5.2 спецификации).

Календарные вычисления («пятница, 16:30») делаются в локальном часовом поясе, результат
переводится в UTC. Функции чистые: время им передаётся снаружи.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from meetup_bot.services.defaults import (
    DEFAULT_ATTENDANCE_PROMPT_MINUTES_AFTER,
    DEFAULT_CANCEL_DEADLINE_DAYS_BEFORE,
    DEFAULT_CANCEL_DEADLINE_TIME,
    DEFAULT_EVENT_DURATION_MINUTES,
    DEFAULT_EVENT_TIME,
    DEFAULT_EVENT_WEEKDAY,
    DEFAULT_REGISTRATION_CLOSE_MINUTES_AFTER_START,
    DEFAULT_REMINDER_HOURS_BEFORE,
    DEFAULT_TIMEZONE,
)

DAYS_IN_WEEK = 7


class InvalidEventTimes(ValueError):
    """Нарушен инвариант порядка моментов встречи."""


@dataclass(frozen=True)
class ScheduleSettings:
    timezone: str = DEFAULT_TIMEZONE
    event_weekday: int = DEFAULT_EVENT_WEEKDAY
    event_time: time = DEFAULT_EVENT_TIME
    event_duration_minutes: int = DEFAULT_EVENT_DURATION_MINUTES
    cancel_deadline_days_before: int = DEFAULT_CANCEL_DEADLINE_DAYS_BEFORE
    cancel_deadline_time: time = DEFAULT_CANCEL_DEADLINE_TIME
    registration_close_minutes_after_start: int = DEFAULT_REGISTRATION_CLOSE_MINUTES_AFTER_START
    reminder_hours_before: int = DEFAULT_REMINDER_HOURS_BEFORE
    attendance_prompt_minutes_after: int = DEFAULT_ATTENDANCE_PROMPT_MINUTES_AFTER


@dataclass(frozen=True)
class EventTimes:
    starts_at: datetime
    ends_at: datetime
    cancel_deadline_at: datetime
    registration_closes_at: datetime
    reminder_at: datetime
    attendance_prompt_at: datetime


def next_event_start(now: datetime, settings: ScheduleSettings) -> datetime:
    """Ближайшее будущее начало встречи (UTC) по дню недели и времени из настроек."""
    tz = ZoneInfo(settings.timezone)
    local_now = now.astimezone(tz)
    days_ahead = (settings.event_weekday - local_now.weekday()) % DAYS_IN_WEEK
    candidate = datetime.combine(
        local_now.date() + timedelta(days=days_ahead), settings.event_time, tzinfo=tz
    )
    if candidate <= local_now:
        candidate += timedelta(days=DAYS_IN_WEEK)
    return candidate.astimezone(UTC)


def compute_times(starts_at: datetime, settings: ScheduleSettings) -> EventTimes:
    """Вычисляет моменты встречи (5.2).

    Дедлайн отмены не позже начала; запрос явки не раньше закрытия записи: пока можно
    записаться, отмечать явку рано.
    """
    if starts_at.tzinfo is None:
        raise ValueError("starts_at должен быть aware-datetime")
    tz = ZoneInfo(settings.timezone)
    starts_utc = starts_at.astimezone(UTC)
    deadline_date = starts_utc.astimezone(tz).date() - timedelta(
        days=settings.cancel_deadline_days_before
    )
    deadline = datetime.combine(deadline_date, settings.cancel_deadline_time, tzinfo=tz)
    closes = starts_utc + timedelta(minutes=settings.registration_close_minutes_after_start)
    prompt = starts_utc + timedelta(minutes=settings.attendance_prompt_minutes_after)
    return EventTimes(
        starts_at=starts_utc,
        ends_at=starts_utc + timedelta(minutes=settings.event_duration_minutes),
        cancel_deadline_at=min(deadline.astimezone(UTC), starts_utc),
        registration_closes_at=closes,
        reminder_at=starts_utc - timedelta(hours=settings.reminder_hours_before),
        attendance_prompt_at=max(prompt, closes),
    )


def validate_times(times: EventTimes, created_at: datetime) -> None:
    """Порядок: создание < дедлайн отмены <= начало <= закрытие записи <= запрос явки.

    Начало строго раньше конца встречи и запроса явки.
    """
    ordered = (
        created_at < times.cancel_deadline_at,
        times.cancel_deadline_at <= times.starts_at,
        times.starts_at <= times.registration_closes_at,
        times.registration_closes_at <= times.attendance_prompt_at,
        times.starts_at < times.attendance_prompt_at,
        times.starts_at < times.ends_at,
    )
    if not all(ordered):
        raise InvalidEventTimes(
            "нарушен порядок: создание < дедлайн отмены <= начало <= закрытие записи "
            "<= запрос явки, начало < конец"
        )
