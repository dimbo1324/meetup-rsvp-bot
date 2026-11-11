"""Единственный источник времени: в тестах и в dev-режиме подменяется."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Текущий момент как aware-datetime в UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Управляемые часы для тестов и команды `/dev_shift`."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock требует aware-datetime")
        self._current = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


class ShiftableClock:
    """Системные часы со сдвигом для dev-команды `/dev_shift`; в бою сдвиг всегда нулевой."""

    def __init__(self, base: Clock) -> None:
        self._base = base
        self._shift = timedelta(0)

    def now(self) -> datetime:
        return self._base.now() + self._shift

    def shift(self, delta: timedelta) -> None:
        self._shift += delta

    @property
    def offset(self) -> timedelta:
        return self._shift
