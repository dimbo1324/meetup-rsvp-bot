"""Реестр блокировок встреч: взаимное исключение и отсутствие утечки записей."""

import asyncio

from meetup_bot.services.locks import EventLocks


async def test_блокировка_одной_встречи_исключает_параллельных() -> None:
    locks = EventLocks()
    inside = 0
    peak = 0

    async def work() -> None:
        nonlocal inside, peak
        async with locks.lock(1):
            inside += 1
            peak = max(peak, inside)
            await asyncio.sleep(0)
            inside -= 1

    await asyncio.gather(*(work() for _ in range(10)))
    assert peak == 1


async def test_разные_встречи_не_блокируют_друг_друга() -> None:
    locks = EventLocks()
    async with locks.lock(1):
        async with asyncio.timeout(1):
            async with locks.lock(2):
                assert len(locks) == 2


async def test_после_серии_разных_встреч_реестр_пуст() -> None:
    locks = EventLocks()
    for event_id in range(500):
        async with locks.lock(event_id):
            pass
    assert len(locks) == 0


async def test_запись_живёт_пока_есть_ожидающие() -> None:
    locks = EventLocks()
    release = asyncio.Event()

    async def holder() -> None:
        async with locks.lock(7):
            await release.wait()

    async def waiter() -> None:
        async with locks.lock(7):
            pass

    first = asyncio.create_task(holder())
    await asyncio.sleep(0)
    second = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert len(locks) == 1
    release.set()
    await asyncio.gather(first, second)
    assert len(locks) == 0


async def test_отменённый_ожидающий_не_оставляет_запись() -> None:
    locks = EventLocks()
    release = asyncio.Event()

    async def holder() -> None:
        async with locks.lock(3):
            await release.wait()

    async def waiter() -> None:
        async with locks.lock(3):
            pass

    first = asyncio.create_task(holder())
    await asyncio.sleep(0)
    second = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    second.cancel()
    release.set()
    await asyncio.gather(first, second, return_exceptions=True)
    assert len(locks) == 0


async def test_блокировка_берётся_повторно_после_освобождения() -> None:
    locks = EventLocks()
    for _ in range(3):
        async with locks.lock(5):
            assert len(locks) == 1
    assert len(locks) == 0
