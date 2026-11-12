"""Отмена задач цикла опроса: без утечек, зависших фоновых задач и забытых блокировок."""

from __future__ import annotations

import asyncio

from tests.unit.loop.harness import make_rig

from rcbot.domain.errors import SessionExpired

EMPTY: frozenset = frozenset()  # type: ignore[type-arg]


def pending_tasks() -> set[asyncio.Task[object]]:
    current = asyncio.current_task()
    return {t for t in asyncio.all_tasks() if t is not current and not t.done()}


async def test_отмена_во_время_проверки_ничего_не_оставляет() -> None:
    rig = make_rig()

    async def hang() -> frozenset:  # type: ignore[type-arg]
        await rig.clock.sleep(10_000)
        return EMPTY

    rig.source.push(hang)
    baseline = pending_tasks()
    await rig.start()
    assert rig.loop.tick_started_at is not None
    await rig.kill()
    assert rig.task is not None and rig.task.cancelled()
    assert rig.loop.tick_started_at is None
    assert rig.loop.last_tick_finished_at is not None
    assert rig.clock.sleeping == 0
    assert pending_tasks() == baseline


async def test_отмена_во_сне_ничего_не_оставляет() -> None:
    rig = make_rig([EMPTY])
    baseline = pending_tasks()
    await rig.start()
    assert rig.clock.sleeping == 1
    await rig.kill()
    assert rig.clock.sleeping == 0
    assert pending_tasks() == baseline


async def test_отмена_во_время_паузы_ничего_не_оставляет() -> None:
    from datetime import timedelta

    from tests.fakes import T0

    rig = make_rig([EMPTY], paused_until=T0 + timedelta(days=5))
    baseline = pending_tasks()
    await rig.start()
    await rig.kill()
    assert rig.clock.sleeping == 0
    assert pending_tasks() == baseline


async def test_отмена_во_время_входа_освобождает_блокировку_и_не_ломает_повторный_вход() -> None:
    calls = 0

    async def first_hangs() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await rig.clock.sleep(10_000)

    rig = make_rig([SessionExpired("x"), EMPTY], auth_delay=first_hangs)
    baseline = pending_tasks()
    await rig.start()
    assert rig.auth.concurrent == 1  # вход идёт
    await rig.kill()
    assert rig.auth.concurrent == 0
    assert rig.session.has_session is False
    assert rig.clock.sleeping == 0
    assert pending_tasks() == baseline
    # блокировка свободна: следующий вход проходит, а не виснет
    await asyncio.wait_for(rig.session.relogin("после отмены"), 1)
    assert rig.session.has_session


async def test_stop_во_время_проверки_дожидается_её_и_завершает_run() -> None:
    rig = make_rig()

    async def slow() -> frozenset:  # type: ignore[type-arg]
        await rig.clock.sleep(5)
        return EMPTY

    rig.source.push(slow, EMPTY)
    await rig.start()
    await rig.advance(1)
    rig.loop.stop()
    await rig.advance(0)
    assert rig.task is not None and not rig.task.done()  # проверка ещё идёт
    await rig.advance(4)
    assert rig.task.done() and rig.task.exception() is None
    assert len(rig.source.starts) == 1
    assert rig.loop.status.checks_total == 1
    assert rig.clock.sleeping == 0


async def test_stop_завершает_run_и_ждёт_фоновые_задачи() -> None:
    # reset_source после таймаута/сбоя идёт фоном; остановка не должна бросать его на полпути
    from rcbot.domain.errors import SiteUnavailable

    rig = make_rig([ValueError("x"), SiteUnavailable("a")])
    closed = asyncio.Event()

    async def slow_close() -> None:
        await rig.clock.sleep(1)
        closed.set()

    rig.source.close = slow_close  # type: ignore[method-assign]
    await rig.start()
    task = rig.task
    assert task is not None
    rig.loop.stop()
    await rig.advance(1)
    assert task.done()
    assert closed.is_set()
