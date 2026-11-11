"""Ограничитель частоты уведомлений: не чаще одного раза за интервал на ключ.

Состояние в памяти: после перезапуска процесса ограничение сбрасывается, для защиты
служебного чата от спама этого достаточно.
"""

from __future__ import annotations

from collections.abc import Hashable
from datetime import datetime, timedelta

from meetup_bot.clock import Clock

# Предел числа запомненных ключей: злоумышленник с множеством аккаунтов не раздует память
MAX_COOLDOWN_KEYS = 1000


class Cooldown:
    def __init__(
        self, interval: timedelta, clock: Clock, *, max_keys: int = MAX_COOLDOWN_KEYS
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._max_keys = max_keys
        # Порядок вставки совпадает с порядком времени: ключ при обновлении переставляется в конец
        self._last: dict[Hashable, datetime] = {}

    def allow(self, key: Hashable) -> bool:
        """True и запоминает момент, если с прошлого раза по ключу прошло достаточно времени."""
        now = self._clock.now()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return False
        self._last.pop(key, None)
        self._last[key] = now
        if len(self._last) > self._max_keys:
            self._evict(now)
        return True

    def _evict(self, now: datetime) -> None:
        # Старейшие записи идут первыми: сначала выбрасываем истёкшие, затем самые давние
        for stale in [k for k, at in self._last.items() if now - at >= self._interval]:
            del self._last[stale]
        while len(self._last) > self._max_keys:
            del self._last[next(iter(self._last))]

    def __len__(self) -> int:
        return len(self._last)
