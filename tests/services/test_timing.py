from dataclasses import replace
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
# Пятница 23.10.2026 16:30 в Монтевидео (UTC−3)
FRIDAY_START = datetime(2026, 10, 23, 19, 30, tzinfo=UTC)
CREATED_AT = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_пятница_16_30_в_монтевидео_это_19_30_utc() -> None:  # кейс 17
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    assert next_event_start(now, SETTINGS) == FRIDAY_START


def test_в_пятницу_после_начала_берётся_следующая_неделя() -> None:
    now = FRIDAY_START + timedelta(minutes=1)
    assert next_event_start(now, SETTINGS) == FRIDAY_START + timedelta(days=7)


def test_моменты_по_умолчанию() -> None:
    times = compute_times(FRIDAY_START, SETTINGS)
    assert times.starts_at == FRIDAY_START
    # Встреча 16:30–18:30
    assert times.ends_at == datetime(2026, 10, 23, 21, 30, tzinfo=UTC)
    # Четверг 20:00 по Монтевидео
    assert times.cancel_deadline_at == datetime(2026, 10, 22, 23, 0, tzinfo=UTC)
    # Запись открыта ещё час после начала: опоздавшие могут подойти
    assert times.registration_closes_at == datetime(2026, 10, 23, 20, 30, tzinfo=UTC)
    # Четверг 16:30 — за сутки до начала
    assert times.reminder_at == datetime(2026, 10, 22, 19, 30, tzinfo=UTC)
    # Запрос явки через 90 минут после начала: 18:00 по Монтевидео
    assert times.attendance_prompt_at == datetime(2026, 10, 23, 21, 0, tzinfo=UTC)


def test_длительность_встречи_берётся_из_настроек() -> None:
    times = compute_times(FRIDAY_START, replace(SETTINGS, event_duration_minutes=45))
    assert times.ends_at == FRIDAY_START + timedelta(minutes=45)


def test_закрытие_записи_в_момент_начала_при_нуле_минут() -> None:
    times = compute_times(FRIDAY_START, replace(SETTINGS, registration_close_minutes_after_start=0))
    assert times.registration_closes_at == times.starts_at
    validate_times(times, CREATED_AT)


def test_запрос_явки_не_раньше_закрытия_записи() -> None:
    settings = replace(SETTINGS, registration_close_minutes_after_start=120)
    times = compute_times(FRIDAY_START, settings)
    # 16:30 + 120 минут = 18:30: запрос явки сдвигается с 18:00 на момент закрытия
    assert times.registration_closes_at == datetime(2026, 10, 23, 21, 30, tzinfo=UTC)
    assert times.attendance_prompt_at == times.registration_closes_at
    validate_times(times, CREATED_AT)


def test_запрос_явки_позже_закрытия_не_сдвигается() -> None:
    settings = replace(SETTINGS, attendance_prompt_minutes_after=300)
    times = compute_times(FRIDAY_START, settings)
    assert times.attendance_prompt_at == FRIDAY_START + timedelta(minutes=300)


def test_дедлайн_отмены_не_позже_начала() -> None:
    settings = replace(SETTINGS, cancel_deadline_days_before=0, cancel_deadline_time=time(23, 0))
    times = compute_times(FRIDAY_START, settings)
    assert times.cancel_deadline_at == times.starts_at
    validate_times(times, CREATED_AT)


def test_инварианты_порядка() -> None:
    times = compute_times(FRIDAY_START, SETTINGS)
    validate_times(times, CREATED_AT)
    with pytest.raises(InvalidEventTimes):
        validate_times(times, times.cancel_deadline_at + timedelta(minutes=1))


@pytest.mark.parametrize(
    "broken",
    [
        # Дедлайн отмены после начала: отменять можно было бы уже на самой встрече
        {"cancel_deadline_at": FRIDAY_START + timedelta(minutes=1)},
        # Закрытие записи раньше начала больше не допускается: запись идёт и после начала
        {"registration_closes_at": FRIDAY_START - timedelta(minutes=1)},
        # Запрос явки раньше закрытия: отмечали бы явку, пока ещё можно записаться
        {"attendance_prompt_at": FRIDAY_START + timedelta(minutes=59)},
        {"ends_at": FRIDAY_START},
    ],
)
def test_нарушение_порядка_отвергается(broken: dict[str, datetime]) -> None:
    times = replace(compute_times(FRIDAY_START, SETTINGS), **broken)
    with pytest.raises(InvalidEventTimes):
        validate_times(times, CREATED_AT)


def test_запрос_явки_строго_после_начала() -> None:
    times = replace(
        compute_times(FRIDAY_START, SETTINGS),
        registration_closes_at=FRIDAY_START,
        attendance_prompt_at=FRIDAY_START,
    )
    with pytest.raises(InvalidEventTimes):
        validate_times(times, CREATED_AT)


def test_naive_datetime_отвергается() -> None:
    with pytest.raises(ValueError):
        compute_times(datetime(2026, 10, 23, 16, 30), SETTINGS)
