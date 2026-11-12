"""Общие данные и приёмы сквозных сценариев."""

from __future__ import annotations

import asyncio
from datetime import date, time
from itertools import pairwise
from typing import Any

from rcbot.domain.events import DomainEvent
from rcbot.domain.models import Channel, Slot
from tests.e2e.site import FakeSite
from tests.e2e.stand import Stand
from tests.fakes import FakeNotifier

D23 = ["2027-02-23T10:30", "2027-02-23T10:45", "2027-02-23T11:00"]
D24 = ["2027-02-24T09:00"]
D25 = ["2027-02-25T09:30"]


def slot(day: int, hour: int | None = None, minute: int = 0, month: int = 2) -> Slot:
    return Slot(date(2027, month, day), None if hour is None else time(hour, minute))


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
