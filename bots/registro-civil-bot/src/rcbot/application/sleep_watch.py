"""Сон ноутбука: как отличить его от зависания и что о нём помнить до отправки сообщения.

Сравниваются два промежутка между опросами: по стенным часам и по времени бодрствования системы
(порт `AwakeClock`). Во сне первый растёт, второй стоит; разница — длительность сна. Настоящее
зависание цикла событий её не создаёт: система бодрствует, оба промежутка растут одинаково.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from rcbot.application.ports import Clock

# Секунды бодрствования с произвольной точки отсчёта (`infrastructure/win/sleep.py`)
AwakeClock = Callable[[], float]

# Короче этого — погрешность часов и усыпления на мгновение: ни сон, ни его признак не фиксируем
SLEEP_MIN_SEC = 120.0
# Владельцу сообщаем о сне не короче этого: кратковременное фоновое пробуждение — не повод писать
SLEEP_NOTICE_MIN_SEC = 300.0
# Сколько раз пробуем доставить сообщение о сне (дальше остаётся запись в журнале)
SLEEP_NOTICE_MAX_TRIES = 6


@dataclass(frozen=True)
class SleepSpan:
    """Один обнаруженный сон: приблизительно с `started` до `ended`, `slept_sec` секунд."""

    started: datetime
    ended: datetime
    slept_sec: float


@dataclass
class SleepNotice:
    """Что владельцу ещё не сообщено: несколько снов подряд объединяются в одно сообщение."""

    started: datetime
    ended: datetime
    slept_sec: float
    spans: int = 1
    tries: int = 0

    def merge(self, span: SleepSpan) -> None:
        self.started = min(self.started, span.started)
        self.ended = max(self.ended, span.ended)
        self.slept_sec += span.slept_sec
        self.spans += 1


class SleepWatch:
    def __init__(self, clock: Clock, awake: AwakeClock) -> None:
        self._clock = clock
        self._awake = awake
        self._wall = clock.now()
        self._awake_at = awake()

    def poll(self) -> SleepSpan | None:
        """Сон между прошлым вызовом и этим, если он был не короче `SLEEP_MIN_SEC`."""
        wall, awake = self._clock.now(), self._awake()
        slept = (wall - self._wall).total_seconds() - (awake - self._awake_at)
        self._wall, self._awake_at = wall, awake
        if slept < SLEEP_MIN_SEC:
            return None
        return SleepSpan(wall - timedelta(seconds=slept), wall, slept)
