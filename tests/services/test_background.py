"""Фоновые уведомления: ответ не ждёт Telegram, ошибки логируются, остановка не теряет задачи."""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from meetup_bot.clock import FakeClock
from meetup_bot.services.safe import BackgroundTasks
from tests.helpers import OWNER_ID, App, make_app, person


async def test_spawn_выполняет_корутину_в_фоне() -> None:
    background = BackgroundTasks()
    done: list[int] = []

    async def effect() -> None:
        done.append(1)

    background.spawn(effect())
    assert done == []
    await background.drain()
    assert done == [1]
    assert background.pending == 0


async def test_ошибка_фоновой_задачи_логируется_и_не_ломает_остальные(
    caplog: pytest.LogCaptureFixture,
) -> None:
    background = BackgroundTasks()
    done: list[int] = []

    async def boom() -> None:
        raise RuntimeError("сеть")

    async def fine() -> None:
        done.append(1)

    background.spawn(boom())
    background.spawn(fine())
    with caplog.at_level(logging.ERROR):
        await background.drain()
    assert done == [1]
    assert any("фоновое уведомление" in record.message for record in caplog.records)


async def test_drain_ждёт_и_порождённые_по_ходу_задачи() -> None:
    background = BackgroundTasks()
    done: list[str] = []

    async def child() -> None:
        done.append("child")

    async def parent() -> None:
        await asyncio.sleep(0)
        background.spawn(child())
        done.append("parent")

    background.spawn(parent())
    await background.drain()
    assert done == ["parent", "child"]


async def test_aclose_дожидается_задач() -> None:
    background = BackgroundTasks()
    done: list[int] = []

    async def effect() -> None:
        await asyncio.sleep(0.01)
        done.append(1)

    background.spawn(effect())
    await background.aclose()
    assert done == [1]


async def test_aclose_по_таймауту_отменяет_зависшие(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("meetup_bot.services.safe.BACKGROUND_CLOSE_TIMEOUT", 0.01)
    background = BackgroundTasks()

    async def hang() -> None:
        await asyncio.Event().wait()

    background.spawn(hang())
    await background.aclose()
    await asyncio.sleep(0)
    assert background.pending == 0


# ---- запись не ждёт уведомлений -------------------------------------------------------


@pytest.fixture
async def deferred_app(tmp_path: Path, clock: FakeClock) -> App:
    """Приложение с настоящим фоновым реестром: уведомления отправляются не сразу."""
    return await make_app(tmp_path, clock, background=BackgroundTasks())


def gate_service_chat(app: App, monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    gate = asyncio.Event()
    original = app.notifier.send_service

    async def gated(text: str) -> bool:
        await gate.wait()
        return await original(text)

    monkeypatch.setattr(app.notifier, "send_service", gated)
    return gate


async def test_запись_возвращается_не_дожидаясь_служебного_чата(
    deferred_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = deferred_app
    gate = gate_service_chat(app, monkeypatch)
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=1)).event_id
    async with asyncio.timeout(5):
        result = await app.services.registration.register(event_id, person(1))
    assert result.filled_event
    assert app.notifier.service == []
    assert app.services.background.pending == 1
    gate.set()
    await app.services.background.drain()
    assert len(app.notifier.service) == 1


async def test_отмена_возвращается_не_дожидаясь_уведомлений(
    deferred_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = deferred_app
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=10)).event_id
    await app.services.registration.register(event_id, person(1))
    gate = gate_service_chat(app, monkeypatch)
    async with asyncio.timeout(5):
        await app.services.registration.cancel(event_id, person(1).id)
    assert app.notifier.service == []
    gate.set()
    await app.services.background.drain()
    assert len(app.notifier.service) == 1


# ---- защита служебного чата от спама ----------------------------------------------------


async def create_event(app: App, capacity: int) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)).event_id


def cancel_messages(app: App) -> list[str]:
    return [text for text in app.notifier.service if "отменил(а) запись" in text]


def full_messages(app: App) -> list[str]:
    return [text for text in app.notifier.service if "заполнена" in text]


async def test_повторная_отмена_одного_человека_не_спамит_чат(app: App) -> None:
    event_id = await create_event(app, capacity=10)
    for _ in range(3):
        await app.services.registration.register(event_id, person(1))
        await app.services.registration.cancel(event_id, person(1).id)
    assert len(cancel_messages(app)) == 1


async def test_после_интервала_отмена_снова_уведомляет(app: App) -> None:
    event_id = await create_event(app, capacity=10)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.cancel(event_id, person(1).id)
    app.clock.advance(timedelta(minutes=10))
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.cancel(event_id, person(1).id)
    assert len(cancel_messages(app)) == 2


async def test_отмены_разных_людей_уведомляют_каждую(app: App) -> None:
    event_id = await create_event(app, capacity=10)
    for n in (1, 2):
        await app.services.registration.register(event_id, person(n))
        await app.services.registration.cancel(event_id, person(n).id)
    assert len(cancel_messages(app)) == 2


async def test_заполнение_не_уведомляет_чаще_интервала_на_встречу(app: App) -> None:
    event_id = await create_event(app, capacity=1)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.cancel(event_id, person(1).id)
    await app.services.registration.register(event_id, person(2))
    assert len(full_messages(app)) == 1
    await app.services.registration.cancel(event_id, person(2).id)
    app.clock.advance(timedelta(minutes=10))
    await app.services.registration.register(event_id, person(3))
    assert len(full_messages(app)) == 2


async def test_заполнение_разных_встреч_уведомляет_каждую(app: App) -> None:
    first = await create_event(app, capacity=1)
    second = (
        await app.services.events.create_event_as_admin(
            OWNER_ID, capacity=1, starts_at=app.clock.now() + timedelta(days=9)
        )
    ).event_id
    await app.services.registration.register(first, person(1))
    await app.services.registration.register(second, person(2))
    assert len(full_messages(app)) == 2
