"""Supervisor: упавшая задача перезапускается с паузой 5, 10, … до 300 с; остановка — по порядку."""

from __future__ import annotations

import asyncio
import logging

import pytest
from tests.fakes import ManualClock, settle

from rcbot.application.supervisor import RESTART_MAX_SEC, RESTART_MIN_SEC, Supervisor


class Crasher:
    """Фабрика задачи: каждый запуск записывает момент и падает (или работает заданное время)."""

    def __init__(self, clock: ManualClock, *, run_for: list[float] | None = None) -> None:
        self.clock = clock
        self.starts: list[float] = []
        self._run_for = run_for or []

    async def __call__(self) -> None:
        self.starts.append(self.clock.monotonic())
        if self._run_for:
            await self.clock.sleep(self._run_for.pop(0))
        raise RuntimeError("сбой задачи")


async def test_упавшая_задача_перезапускается_с_паузой_5_10_до_300() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    crasher = Crasher(clock)
    sup.start("poll", crasher)
    await settle()
    await clock.advance(5 + 10 + 20 + 40 + 80 + 160 + 300 + 300)
    starts = [round(s - 1000.0) for s in crasher.starts]
    assert starts == [0, 5, 15, 35, 75, 155, 315, 615, 915]
    assert RESTART_MIN_SEC == 5.0 and RESTART_MAX_SEC == 300.0
    await sup.stop()


async def test_пауза_не_короче_5_с_и_не_длиннее_300_с() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    sup.start("x", Crasher(clock))
    await settle()
    await clock.advance(5000)
    assert min(clock.sleeps) == 5.0 and max(clock.sleeps) == 300.0
    await sup.stop()


async def test_после_долгой_здоровой_работы_пауза_снова_5_с() -> None:
    # Задача проработала час и упала: это новый сбой, а не продолжение серии — ждать 5 минут
    # (пока опрос стоит) незачем.
    clock = ManualClock()
    sup = Supervisor(clock)
    crasher = Crasher(clock, run_for=[0, 0, 0, 3600, 0])
    sup.start("poll", crasher)
    await settle()
    await clock.advance(5 + 10 + 20 + 3600)
    await clock.advance(5)
    starts = [round(s - 1000.0) for s in crasher.starts]
    assert starts[:4] == [0, 5, 15, 35]
    assert starts[4] == 35 + 3600 + 5
    await sup.stop()


async def test_нормальное_завершение_не_перезапускается() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    calls = 0

    async def once() -> None:
        nonlocal calls
        calls += 1

    sup.start("once", once)
    await settle()
    await clock.advance(1000)
    assert calls == 1


async def test_restart_false_пробрасывает_исключение_и_не_перезапускает() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    crasher = Crasher(clock)
    sup.start("once", crasher, restart=False)
    await settle()
    await clock.advance(1000)
    assert len(crasher.starts) == 1
    task = sup._tasks["once"]
    assert task.done() and isinstance(task.exception(), RuntimeError)


async def test_исключение_задачи_логируется(caplog: pytest.LogCaptureFixture) -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    with caplog.at_level(logging.WARNING):
        sup.start("poll", Crasher(clock))
        await settle()
        await clock.advance(5)
    assert "задача «poll» упала" in caplog.text
    assert "перезапускается" in caplog.text
    await sup.stop()


async def test_restart_отменяет_работающую_задачу_и_запускает_заново() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    log: list[str] = []

    async def worker() -> None:
        log.append("start")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            log.append("cancelled")
            raise

    sup.start("poll", worker)
    await settle()
    await sup.restart("poll")
    await settle()
    assert log == ["start", "cancelled", "start"]
    await sup.stop()
    assert log == ["start", "cancelled", "start", "cancelled"]


async def test_restart_завершившейся_задачи_просто_запускает_её_снова() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    calls = 0

    async def once() -> None:
        nonlocal calls
        calls += 1

    sup.start("once", once)
    await settle()
    await sup.restart("once")
    await settle()
    assert calls == 2


async def test_restart_сохраняет_параметр_restart_false() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    crasher = Crasher(clock)
    sup.start("once", crasher, restart=False)
    await settle()
    await sup.restart("once")
    await settle()
    await clock.advance(1000)
    assert len(crasher.starts) == 2  # по разу на каждый запуск, без автоперезапусков
    await sup.stop()


async def test_restart_неизвестной_задачи_это_KeyError() -> None:
    with pytest.raises(KeyError):
        await Supervisor(ManualClock()).restart("нет такой")


async def test_restart_в_паузе_после_сбоя_сбрасывает_ожидание() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    crasher = Crasher(clock)
    sup.start("poll", crasher)
    await settle()  # упала, ждёт 5 с
    await sup.restart("poll")
    await settle()
    assert len(crasher.starts) == 2  # запуск сразу, не через 5 с
    assert clock.sleeping == 1  # висит только пауза нового запуска
    await sup.stop()


async def test_restart_не_запускает_вторую_копию_если_старая_не_отменяется() -> None:
    # Два цикла опроса одновременно удвоили бы частоту запросов к сайту
    clock = ManualClock()
    sup = Supervisor(clock)
    release = asyncio.Event()
    running = 0
    peak = 0

    async def stubborn() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # не поддаётся отмене
        running -= 1

    sup.start("poll", stubborn)
    await settle()
    try:
        with pytest.raises(RuntimeError):
            await sup.restart("poll", wait_sec=0.01)
        assert peak == 1
    finally:
        release.set()
        await settle()


async def test_повторный_start_живой_задачи_отклоняется() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)

    async def worker() -> None:
        await asyncio.Event().wait()

    sup.start("poll", worker)
    await settle()
    with pytest.raises(RuntimeError):
        sup.start("poll", worker)
    await sup.stop()


async def test_stop_отменяет_все_задачи_и_паузу_перезапуска() -> None:
    clock = ManualClock()
    sup = Supervisor(clock)
    cancelled: list[str] = []

    def worker(name: str):  # type: ignore[no-untyped-def]
        async def run() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return run

    sup.start("a", worker("a"))
    sup.start("b", worker("b"))
    sup.start("crash", Crasher(clock))
    await settle()
    assert clock.sleeping == 1  # упавшая задача ждёт перезапуска
    await sup.stop()
    assert sorted(cancelled) == ["a", "b"]
    assert clock.sleeping == 0
    await clock.advance(1000)  # после остановки ничего не оживает


async def test_stop_без_задач_и_повторный_stop_безопасны() -> None:
    sup = Supervisor(ManualClock())
    await sup.stop()
    await sup.stop()


async def test_stop_не_ждёт_дольше_таймаута_для_задачи_не_поддающейся_отмене() -> None:
    sup = Supervisor(ManualClock())
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    sup.start("x", stubborn)
    await settle()
    try:
        await asyncio.wait_for(sup.stop(wait_sec=0.02), 1)
    finally:
        release.set()
        await settle()


async def test_wait_done_ждёт_завершения_не_отменяя_задачу(manual_clock) -> None:
    from rcbot.application.supervisor import Supervisor

    sup = Supervisor(manual_clock)
    finished: list[str] = []

    async def worker() -> None:
        await manual_clock.sleep(3)  # досохраняет статистику
        finished.append("готово")

    sup.start("poll", worker)
    waiter = asyncio.create_task(sup.wait_done("poll", 10))
    await manual_clock.advance(2)
    assert not waiter.done() and finished == []  # задачу не отменили и не дожидаются зря
    await manual_clock.advance(2)
    assert await waiter is True and finished == ["готово"]


async def test_wait_done_по_таймауту_возвращает_false_и_не_отменяет(manual_clock) -> None:
    from rcbot.application.supervisor import Supervisor

    sup = Supervisor(manual_clock)

    async def forever() -> None:
        await manual_clock.sleep(10**6)

    sup.start("poll", forever)
    waiter = asyncio.create_task(sup.wait_done("poll", 5))
    await manual_clock.advance(6)
    assert await waiter is False
    await sup.stop()


async def test_wait_done_для_неизвестной_и_завершённой_задачи(manual_clock) -> None:
    from rcbot.application.supervisor import Supervisor

    sup = Supervisor(manual_clock)
    assert await sup.wait_done("нет-такой") is True

    async def quick() -> None:
        return None

    sup.start("poll", quick)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await sup.wait_done("poll") is True
