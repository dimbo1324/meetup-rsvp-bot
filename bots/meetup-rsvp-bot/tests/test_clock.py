from datetime import UTC, datetime, timedelta, timezone

import pytest

from meetup_bot.clock import FakeClock, SystemClock


def test_системные_часы_возвращают_aware_utc() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)


def test_fake_clock_сдвигается(clock: FakeClock) -> None:
    before = clock.now()
    clock.advance(timedelta(minutes=90))
    assert clock.now() - before == timedelta(minutes=90)


def test_fake_clock_приводит_к_utc() -> None:
    local = datetime(2026, 10, 24, 17, 0, tzinfo=timezone(timedelta(hours=-3)))
    assert FakeClock(local).now() == datetime(2026, 10, 24, 20, 0, tzinfo=UTC)


def test_fake_clock_отвергает_naive() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 10, 23, 12, 0))
