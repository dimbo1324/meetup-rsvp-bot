"""Сколько секунд система бодрствовала: время, которое не идёт во сне и гибернации.

Обычные часы (`time.monotonic`, `loop.time()`) на Windows идут и пока ноутбук спит, поэтому по ним
сон не отличить от зависания. `QueryUnbiasedInterruptTime` считает только бодрствование: разница
между временем по стенным часам и этим временем — длительность сна. На других системах
`time.monotonic()` и так не идёт во сне (Linux), поэтому берётся он.
"""

from __future__ import annotations

import logging
import sys
import time

logger = logging.getLogger(__name__)

# Единицы QueryUnbiasedInterruptTime — сотни наносекунд
UNITS_PER_SECOND = 10_000_000


def awake_seconds() -> float:
    """Секунды бодрствования с произвольной точки отсчёта; сбой вызова — как `time.monotonic()`.

    Запасной вариант безопасен: часы, идущие и во сне, дают нулевую длительность сна, то есть
    сон просто не будет замечен, а ложного сообщения не будет.
    """
    if sys.platform == "win32":
        import ctypes

        value = ctypes.c_ulonglong()
        if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
            return value.value / UNITS_PER_SECOND
        logger.warning("QueryUnbiasedInterruptTime не сработал: сон ноутбука замечаться не будет")
    return time.monotonic()
