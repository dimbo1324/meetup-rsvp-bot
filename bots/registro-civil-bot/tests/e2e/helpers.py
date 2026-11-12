"""Общие данные и приёмы сквозных сценариев."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import date
from datetime import time as dtime
from itertools import pairwise
from typing import Any

from rcbot.application.ports import AvailabilitySource, SessionCookie
from rcbot.domain.events import DomainEvent
from rcbot.domain.models import AvailabilitySnapshot, Channel, Slot, SourceKind, WatchTarget
from tests.e2e.site import FakeSite
from tests.e2e.stand import Stand
from tests.fakes import FakeNotifier

D23 = ["2027-02-23T10:30", "2027-02-23T10:45", "2027-02-23T11:00"]
D24 = ["2027-02-24T09:00"]
D25 = ["2027-02-25T09:30"]


def slot(day: int, hour: int | None = None, minute: int = 0, month: int = 2) -> Slot:
    return Slot(date(2027, month, day), None if hour is None else dtime(hour, minute))


def collect_events(stand: Stand) -> list[DomainEvent]:
    """Подписаться на все доменные события: тест видит то же, что рассылка и журнал."""
    seen: list[DomainEvent] = []

    async def on_event(event: DomainEvent) -> None:
        seen.append(event)

    stand.bus.subscribe(DomainEvent, on_event)
    return seen


def slow_channels(delay_sec: float = 0.02) -> dict[Channel, Any]:
    """Каналы, у которых каждая отправка занимает настоящие `delay_sec`: параллельность видна."""

    async def delay() -> None:
        await asyncio.sleep(delay_sec)

    return {channel: FakeNotifier(channel, delay=delay) for channel in Channel}


def gaps(site: FakeSite, last: int) -> list[float]:
    """Интервалы (с по часам стенда) между последними `last` запросами к «сайту»."""
    times = [c.mono for c in site.calls[-last:]]
    return [b - a for a, b in pairwise(times)]


class TimedSource:
    """Запоминает настоящий момент (perf_counter), когда цикл опроса получил снимок с местами."""

    def __init__(self, inner: AvailabilitySource) -> None:
        self.inner = inner
        self.got: list[float] = []

    @property
    def kind(self) -> SourceKind:
        return self.inner.kind

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        snapshot = await self.inner.fetch(target)
        if snapshot.has_slots:
            self.got.append(time.perf_counter())
        return snapshot

    def use_cookies(self, cookies: Sequence[SessionCookie]) -> None:
        self.inner.use_cookies(cookies)

    async def close(self) -> None:
        await self.inner.close()


class NoLogin:
    """Вход не нужен: источник не ходит на сайт."""

    async def login(self) -> list[SessionCookie]:
        return []


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]
