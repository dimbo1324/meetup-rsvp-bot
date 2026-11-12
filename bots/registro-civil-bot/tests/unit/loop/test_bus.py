"""EventBus (SPEC 2.2 п. 4, 2.3): публикация не ждёт подписчиков, сбой одного не задевает других."""

from __future__ import annotations

import asyncio
import logging

import pytest
from tests.fakes import T0, settle, slots, snapshot

from rcbot.application.bus import EventBus
from rcbot.domain.events import (
    DomainEvent,
    ManualActionRequired,
    PollerStateChanged,
    SlotsAppeared,
    SlotsGone,
)
from rcbot.domain.models import PollState


def appeared() -> SlotsAppeared:
    return SlotsAppeared(T0, snapshot(slots(23)))


async def test_подписчик_получает_событие_своего_типа() -> None:
    bus = EventBus()
    got: list[DomainEvent] = []

    async def handler(event: DomainEvent) -> None:
        got.append(event)

    bus.subscribe(SlotsAppeared, handler)
    event = appeared()
    bus.publish(event)
    await settle()
    assert got == [event]


async def test_чужие_типы_событий_не_доходят() -> None:
    bus = EventBus()
    got: list[DomainEvent] = []

    async def handler(event: DomainEvent) -> None:
        got.append(event)

    bus.subscribe(SlotsGone, handler)
    bus.publish(appeared())
    bus.publish(ManualActionRequired(T0, "шаг 1"))
    await settle()
    assert got == []


async def test_подписка_на_базовый_тип_получает_все_подклассы() -> None:
    bus = EventBus()
    got: list[type] = []

    async def handler(event: DomainEvent) -> None:
        got.append(type(event))

    bus.subscribe(DomainEvent, handler)
    bus.publish(appeared())
    bus.publish(PollerStateChanged(T0, PollState.RUNNING, PollState.BACKOFF, "x"))
    await settle()
    assert got == [SlotsAppeared, PollerStateChanged]


async def test_несколько_подписчиков_на_одно_событие_получают_все() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def first(event: DomainEvent) -> None:
        calls.append("first")

    async def second(event: DomainEvent) -> None:
        calls.append("second")

    bus.subscribe(SlotsAppeared, first)
    bus.subscribe(SlotsAppeared, second)
    bus.publish(appeared())
    await settle()
    assert calls == ["first", "second"]


async def test_publish_без_подписчиков_безопасен() -> None:
    EventBus().publish(appeared())


async def test_publish_не_ждёт_подписчика() -> None:
    bus = EventBus()
    release = asyncio.Event()
    finished = False

    async def slow(event: DomainEvent) -> None:
        nonlocal finished
        await release.wait()
        finished = True

    bus.subscribe(SlotsAppeared, slow)
    bus.publish(appeared())  # синхронный вызов: возвращается, пока подписчик ещё стоит
    assert not finished
    release.set()
    await settle()
    assert finished


async def test_медленный_подписчик_не_задерживает_быстрого() -> None:
    bus = EventBus()
    release = asyncio.Event()
    done: list[str] = []

    async def slow(event: DomainEvent) -> None:
        await release.wait()
        done.append("slow")

    async def fast(event: DomainEvent) -> None:
        done.append("fast")

    bus.subscribe(SlotsAppeared, slow)
    bus.subscribe(SlotsAppeared, fast)
    bus.publish(appeared())
    await settle()
    assert done == ["fast"]
    release.set()
    await settle()
    assert done == ["fast", "slow"]


async def test_исключение_подписчика_логируется_и_не_задевает_остальных(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()
    got: list[str] = []

    async def broken(event: DomainEvent) -> None:
        raise RuntimeError("упал подписчик")

    async def healthy(event: DomainEvent) -> None:
        got.append("ok")

    bus.subscribe(SlotsAppeared, broken)
    bus.subscribe(SlotsAppeared, healthy)
    with caplog.at_level(logging.ERROR):
        bus.publish(appeared())
        await settle()
        bus.publish(appeared())  # и шина продолжает работать после сбоя
        await settle()
    assert got == ["ok", "ok"]
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2
    assert "SlotsAppeared" in caplog.text


async def test_подписчик_может_публиковать_новые_события() -> None:
    bus = EventBus()
    got: list[str] = []

    async def relay(event: DomainEvent) -> None:
        bus.publish(SlotsGone(T0, snapshot()))

    async def gone(event: DomainEvent) -> None:
        got.append("gone")

    bus.subscribe(SlotsAppeared, relay)
    bus.subscribe(SlotsGone, gone)
    bus.publish(appeared())
    await settle()
    assert got == ["gone"]


async def test_задачи_подписчиков_не_копятся_после_работы() -> None:
    bus = EventBus()

    async def handler(event: DomainEvent) -> None:
        return None

    bus.subscribe(DomainEvent, handler)
    for _ in range(50):
        bus.publish(appeared())
    await settle()
    assert bus._tasks == set()


# --- drain ---


async def test_drain_без_работы_возвращается_сразу() -> None:
    await asyncio.wait_for(EventBus().drain(10), 0.5)


async def test_drain_ждёт_начатую_работу_подписчиков() -> None:
    bus = EventBus()
    release = asyncio.Event()
    finished = False

    async def handler(event: DomainEvent) -> None:
        nonlocal finished
        await release.wait()
        finished = True

    bus.subscribe(SlotsAppeared, handler)
    bus.publish(appeared())
    drain = asyncio.create_task(bus.drain(5))
    await settle()
    assert not drain.done()  # ждёт, пока подписчик не закончит
    release.set()
    await asyncio.wait_for(drain, 1)
    assert finished


async def test_drain_по_таймауту_отменяет_и_дожидается_отмены() -> None:
    bus = EventBus()
    cancelled = False

    async def never(event: DomainEvent) -> None:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    bus.subscribe(SlotsAppeared, never)
    bus.publish(appeared())
    await settle()
    await bus.drain(0.02)
    assert cancelled  # отмена доведена до конца, задача не висит «наполовину»
    assert bus._tasks == set()


async def test_отмена_подписчика_не_логируется_как_ошибка(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = EventBus()

    async def never(event: DomainEvent) -> None:
        await asyncio.Event().wait()

    bus.subscribe(SlotsAppeared, never)
    bus.publish(appeared())
    await settle()
    with caplog.at_level(logging.ERROR):
        await bus.drain(0.01)
    assert caplog.text == ""
