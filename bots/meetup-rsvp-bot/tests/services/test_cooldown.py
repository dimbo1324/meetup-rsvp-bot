"""Ограничитель частоты уведомлений."""

from datetime import UTC, datetime, timedelta

from meetup_bot.clock import FakeClock
from meetup_bot.services.cooldown import Cooldown

INTERVAL = timedelta(minutes=10)


def make(max_keys: int = 100) -> tuple[Cooldown, FakeClock]:
    clock = FakeClock(datetime(2026, 10, 22, 12, 0, tzinfo=UTC))
    return Cooldown(INTERVAL, clock, max_keys=max_keys), clock


def test_первый_раз_разрешено_повтор_в_интервале_нет() -> None:
    cooldown, clock = make()
    assert cooldown.allow("a")
    clock.advance(INTERVAL - timedelta(seconds=1))
    assert not cooldown.allow("a")


def test_после_интервала_снова_разрешено() -> None:
    cooldown, clock = make()
    assert cooldown.allow("a")
    clock.advance(INTERVAL)
    assert cooldown.allow("a")


def test_запрещённая_попытка_не_продлевает_ожидание() -> None:
    cooldown, clock = make()
    cooldown.allow("a")
    clock.advance(timedelta(minutes=6))
    assert not cooldown.allow("a")
    clock.advance(timedelta(minutes=4))
    assert cooldown.allow("a")


def test_ключи_независимы() -> None:
    cooldown, _ = make()
    assert cooldown.allow((1, 10))
    assert cooldown.allow((2, 10))
    assert cooldown.allow((1, 11))


def test_размер_ограничен_самые_давние_вытесняются() -> None:
    cooldown, clock = make(max_keys=3)
    for key in range(5):
        assert cooldown.allow(key)
        clock.advance(timedelta(seconds=1))
    assert len(cooldown) == 3
    assert not cooldown.allow(4)
    assert cooldown.allow(0)


def test_при_переполнении_сначала_уходят_истёкшие() -> None:
    cooldown, clock = make(max_keys=3)
    for key in range(3):
        cooldown.allow(key)
    clock.advance(INTERVAL)
    cooldown.allow("новый")
    assert len(cooldown) == 1
