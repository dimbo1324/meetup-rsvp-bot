from datetime import UTC, datetime, time, timedelta

import pytest

from meetup_bot.services.timing import (
    InvalidEventTimes,
    ScheduleSettings,
    compute_times,
    next_event_start,
    validate_times,
)

SETTINGS = ScheduleSettings()


def test_суббота_17_00_в_монтевидео_это_20_00_utc() -> None:  # кейс 17
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    assert next_event_start(now, SETTINGS) == datetime(2026, 10, 24, 20, 0, tzinfo=UTC)


def test_в_субботу_после_начала_берётся_следующая_неделя() -> None:
    now = datetime(2026, 10, 24, 21, 0, tzinfo=UTC)
    assert next_event_start(now, SETTINGS) == datetime(2026, 10, 31, 20, 0, tzinfo=UTC)


def test_моменты_по_умолчанию() -> None:
    times = compute_times(datetime(2026, 10, 24, 20, 0, tzinfo=UTC), SETTINGS)
    # пятница 20:00 по Монтевидео
    assert times.cancel_deadline_at == datetime(2026, 10, 23, 23, 0, tzinfo=UTC)
    assert times.registration_closes_at == datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
    assert times.reminder_at == datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
    assert times.attendance_prompt_at == datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def test_дедлайн_отмены_не_позже_закрытия_записи() -> None:
    settings = ScheduleSettings(cancel_deadline_days_before=0, cancel_deadline_time=time(23, 0))
    times = compute_times(datetime(2026, 10, 24, 20, 0, tzinfo=UTC), settings)
    assert times.cancel_deadline_at == times.registration_closes_at


def test_инварианты_порядка() -> None:
    times = compute_times(datetime(2026, 10, 24, 20, 0, tzinfo=UTC), SETTINGS)
    validate_times(times, datetime(2026, 10, 23, 12, 0, tzinfo=UTC))
    with pytest.raises(InvalidEventTimes):
        validate_times(times, times.cancel_deadline_at + timedelta(minutes=1))


def test_naive_datetime_отвергается() -> None:
    with pytest.raises(ValueError):
        compute_times(datetime(2026, 10, 24, 20, 0), SETTINGS)


def test_по_умолчанию_запись_закрывается_в_момент_начала() -> None:
    assert ScheduleSettings().registration_close_hours_before == 0
    times = compute_times(datetime(2026, 10, 24, 20, 0, tzinfo=UTC), SETTINGS)
    assert times.registration_closes_at == times.starts_at


def test_без_автозакрытия_дедлайн_отмены_остаётся_днём_до_в_20_00() -> None:
    times = compute_times(datetime(2026, 10, 24, 20, 0, tzinfo=UTC), SETTINGS)
    assert times.cancel_deadline_at == datetime(2026, 10, 23, 23, 0, tzinfo=UTC)


def test_инварианты_выдерживают_равенство_закрытия_и_начала() -> None:
    times = compute_times(datetime(2026, 10, 24, 20, 0, tzinfo=UTC), SETTINGS)
    validate_times(times, datetime(2026, 10, 23, 12, 0, tzinfo=UTC))


def test_дедлайн_отмены_в_день_встречи_без_автозакрытия_ограничен_началом() -> None:
    settings = ScheduleSettings(cancel_deadline_days_before=0, cancel_deadline_time=time(23, 0))
    times = compute_times(datetime(2026, 10, 24, 20, 0, tzinfo=UTC), settings)
    assert times.cancel_deadline_at == times.registration_closes_at == times.starts_at
    validate_times(times, datetime(2026, 10, 23, 12, 0, tzinfo=UTC))
