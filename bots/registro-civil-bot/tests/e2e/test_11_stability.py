"""Сценарий 11: устойчивость — сутки проверок, утечки задач и памяти, квота журнала, остановка."""

from __future__ import annotations

import asyncio
import gc
import tracemalloc
from datetime import timedelta

import pytest

from rcbot.application import housekeeping
from rcbot.domain.models import Channel, SourceKind
from tests.e2e.helpers import NoLogin
from tests.e2e.stand import Stand, fake_source
from tests.fakes import slots

DAY_TICKS = 1440  # сутки при интервале 60 с
STEADY = {"ladder_age_hours": 20, "poll_interval_sec": 60}


def script(ticks: int, every: int = 50) -> list:
    """Каждые `every` проверок появляются места, затем три пустые — инцидент закрывается."""
    return [slots(23) if i % every == 0 else slots() for i in range(ticks)]


async def build(make_stand, stand_clock, ticks: int, **kwargs) -> Stand:
    source = fake_source(stand_clock, *script(ticks), kind=SourceKind.API)
    return await make_stand(primary=source, auth=NoLogin(), **{**STEADY, **kwargs})


@pytest.mark.slow
async def test_сутки_проверок_без_утечек_с_почасовой_статистикой_и_журналом(
    make_stand, stand_clock
) -> None:
    tracemalloc.start()
    try:
        stand = await build(make_stand, stand_clock, DAY_TICKS)
        handlers = len(stand.bus._handlers)
        await stand.checks(199)  # 200 проверок: всё «прогрето»
        gc.collect()
        tasks_early = len(asyncio.all_tasks())
        memory_early = tracemalloc.get_traced_memory()[0]

        await stand.checks(DAY_TICKS - 200)
        gc.collect()
        tasks_late = len(asyncio.all_tasks())
        memory_late = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    status = stand.loop.status
    assert status.checks_total == DAY_TICKS and status.errors_total == 0
    # задачи и подписчики не копятся
    assert tasks_late <= tasks_early, f"задач стало больше: {tasks_early} → {tasks_late}"
    assert len(stand.bus._handlers) == handlers
    assert not stand.bus._tasks and not stand.alerts._background and not stand.loop._background
    # память: прирост на 1240 проверок — не больше нескольких килобайт на проверку (списки
    # вызовов самих фейков тоже растут, поэтому порог с запасом)
    growth = memory_late - memory_early
    print(f"прирост памяти за {DAY_TICKS - 200} проверок: {growth / 1024:.0f} КБ")
    assert growth < 4 * 1024 * 1024

    # почасовая статистика: по строке на час, а не на проверку (SPEC 2.7)
    rows = await stand.storage.stats.since(stand.clock.now() - timedelta(days=2))
    assert len(rows) == 23
    assert all(h.checks == 60 for h in rows)
    last_full = stand.clock.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    assert rows[-1].hour == last_full
    current = stand.loop.current_hour()
    assert current is not None and current.checks == 60 and len(rows) + 1 == 24

    # журнал за сутки: ровно по строке на проверку; граница суток — полночь по Монтевидео
    stand.journal.flush()
    lines = {
        path.name: len(path.read_text("utf-8").splitlines())
        for path in sorted((stand.journal.root / "checks").glob("*.jsonl"))
    }
    assert lines == {"checks-2026-11-12.jsonl": 720, "checks-2026-11-13.jsonl": 720}
    assert len(stand.journal.read_openings()) == 2 * 29  # появились + пропали, по 29 раз
    assert stand.journal.read_status()["checks_total"] == DAY_TICKS


@pytest.mark.slow
async def test_квота_журнала_срабатывает_по_ходу_работы_и_не_трогает_сегодняшнее(
    make_stand, stand_clock
) -> None:
    stand = await build(
        make_stand,
        stand_clock,
        400,
        poll_interval_sec=600,
        journal_max_bytes=60_000,
        journal_trim_bytes=30_000,
        start=False,
    )
    stand.start(quota=True, liveness=False)
    await stand.quiesce()
    await stand.advance(600 * 399)  # около 2,8 суток при проверке раз в 10 минут
    # очистка идёт в потоке и стенд её не ждёт: доводим последний круг явно, без гонки
    stand.journal.flush()
    await stand.housekeeping.journal_quota_once()

    stand.journal.flush()
    names = sorted(p.name for p in (stand.journal.root / "checks").glob("*.jsonl"))
    today = stand.clock.now().astimezone(stand.settings.tz).date().isoformat()
    assert f"checks-{today}.jsonl" in names, "сегодняшний файл не удаляется"
    assert "checks-2026-11-12.jsonl" not in names, "самый старый день удалён"
    assert (stand.journal.root / "openings.csv").exists()
    stand.journal.flush()  # фоновая запись дописана
    quota_events = [e for e in stand.journal.read_events() if e["code"] == "journal.quota"]
    assert quota_events and all(e["level"] == "WARNING" for e in quota_events)
    assert stand.journal.size() <= 60_000, "после очистки размер не больше квоты"
    # сегодняшний файл цел: строка на каждую проверку этого дня (раз в 10 минут от полуночи)
    local = stand.clock.now().astimezone(stand.settings.tz)
    todays = (stand.journal.root / "checks" / f"checks-{today}.jsonl").read_text("utf-8")
    assert len(todays.splitlines()) == (local.hour * 60 + local.minute) // 10 + 1


async def test_квота_не_срабатывает_пока_размер_в_пределах(make_stand, stand_clock) -> None:
    stand = await build(make_stand, stand_clock, 50, start=False)
    stand.start(quota=True)
    await stand.quiesce()
    await stand.advance(3600)
    assert not [e for e in stand.journal.read_events() if e["code"] == "journal.quota"]
    assert housekeeping.JOURNAL_QUOTA_EVERY_SEC == 600


async def test_остановка_закрывает_всё_и_не_оставляет_задач(make_stand, stand_clock) -> None:
    before = asyncio.all_tasks()
    stand = await build(make_stand, stand_clock, 30)
    await stand.checks(10)
    await stand.stop()
    await asyncio.sleep(0)

    assert not {t for t in asyncio.all_tasks() - before if not t.done()}
    assert stand.primary.closed >= 1  # type: ignore[attr-defined]
    assert all(spy.inner.closed == 1 for spy in stand.notifiers.values())  # type: ignore[attr-defined]
    assert stand.texts(Channel.TELEGRAM)[-1].startswith("⏹"), "сообщение об остановке ушло"
    await stand.stop()  # повторная остановка безвредна
