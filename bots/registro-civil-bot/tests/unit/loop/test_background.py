"""BackgroundTasks: ссылки на фоновые задачи держатся, упавшая задача видна в логе, drain ждёт."""

from __future__ import annotations

import asyncio
import logging

import pytest
from tests.fakes import settle

from rcbot.application.background import BackgroundTasks


async def test_задача_держится_до_завершения_и_затем_забывается() -> None:
    tasks = BackgroundTasks("тест")
    gate = asyncio.Event()

    async def work() -> None:
        await gate.wait()

    tasks.spawn(work())
    assert len(tasks) == 1 and tasks
    gate.set()
    await settle()
    assert len(tasks) == 0 and not tasks


async def test_упавшая_задача_логируется_только_именем_типа(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Раньше ошибки фоновой записи рассылки терялись молча
    tasks = BackgroundTasks("рассылка")

    async def broken() -> None:
        raise RuntimeError("https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA/1")

    with caplog.at_level(logging.WARNING, logger="rcbot.application.background"):
        tasks.spawn(broken())
        await settle()
    assert "фоновая задача (рассылка) упала: RuntimeError" in caplog.text
    assert "TEST-ETAPA" not in caplog.text
    assert not tasks


async def test_отменённая_задача_не_считается_упавшей(caplog: pytest.LogCaptureFixture) -> None:
    tasks = BackgroundTasks("тест")
    task = tasks.spawn(asyncio.sleep(3600))
    with caplog.at_level(logging.WARNING, logger="rcbot.application.background"):
        task.cancel()
        await settle()
    assert caplog.records == [] and not tasks


async def test_drain_дожидается_всех_задач() -> None:
    tasks = BackgroundTasks("тест")
    done: list[int] = []

    async def work(n: int) -> None:
        await asyncio.sleep(0)
        done.append(n)

    for n in range(3):
        tasks.spawn(work(n))
    await tasks.drain()
    assert sorted(done) == [0, 1, 2] and not tasks


async def test_drain_с_пределом_отменяет_не_успевшие() -> None:
    tasks = BackgroundTasks("тест")
    slow = tasks.spawn(asyncio.sleep(3600))
    await tasks.drain(0.01)
    assert slow.cancelled() and not tasks


async def test_drain_без_задач_сразу_возвращается() -> None:
    await BackgroundTasks("тест").drain(0)
