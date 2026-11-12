"""SqlMaintenance: сроки хранения (SPEC 2.7) и уплотнение файла базы."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from rcbot.config.paths import Paths
from rcbot.domain.models import AppEvent, Channel, DeliveryResult, PollStatsHour
from tests.fakes import T0, slots
from tests.integration.storage.conftest import Db

CUTOFF = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
TICK = timedelta(microseconds=1)


async def count(db: Db, table: str) -> int:
    async with db.engine.connect() as conn:
        return int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar() or 0)


async def test_события_удаляются_строго_старше_границы(db: Db) -> None:
    for moment, code in ((CUTOFF - TICK, "старше"), (CUTOFF, "ровно"), (CUTOFF + TICK, "новее")):
        await db.events.write(AppEvent(moment, "INFO", code, "m"))
    removed = await db.maintenance.delete_older_than({"app_events": CUTOFF})
    assert removed == {"app_events": 1}
    assert {e.code for e in await db.events.last(10, "DEBUG")} == {"ровно", "новее"}


async def test_статистика_удаляется_строго_старше_границы(db: Db) -> None:
    for moment in (CUTOFF - timedelta(hours=1), CUTOFF, CUTOFF + timedelta(hours=1)):
        await db.stats.save_hour(PollStatsHour(moment, 1, 0, 0, [1.0]))
    removed = await db.maintenance.delete_older_than({"poll_stats_hourly": CUTOFF})
    assert removed == {"poll_stats_hourly": 1}
    assert [h.hour for h in await db.stats.since(CUTOFF - timedelta(days=1))] == [
        CUTOFF,
        CUTOFF + timedelta(hours=1),
    ]


async def test_закрытые_инциденты_удаляются_по_времени_открытия_строго_старше(db: Db) -> None:
    for opened in (CUTOFF - TICK, CUTOFF, CUTOFF + TICK):
        incident = await db.incidents.open(db.target.id, opened, slots(1))
        await db.incidents.close(incident.id, opened + timedelta(minutes=5))
    removed = await db.maintenance.delete_older_than({"incidents": CUTOFF})
    assert removed == {"incidents": 1}
    assert sorted(i.opened_at for i in await db.incidents.last(10)) == [CUTOFF, CUTOFF + TICK]


async def test_открытый_инцидент_не_удаляется_сколько_бы_ни_длился(db: Db) -> None:
    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    open_incident = await db.incidents.open(db.target.id, ancient, slots(1, 2))
    closed_incident = await db.incidents.open(db.target.id, ancient, slots(3))
    await db.incidents.close(closed_incident.id, ancient + timedelta(minutes=1))
    removed = await db.maintenance.delete_older_than({"incidents": CUTOFF})
    assert removed == {"incidents": 1}
    current = await db.incidents.current(db.target.id)
    assert current is not None and current.id == open_incident.id
    assert current.slots == slots(1, 2)  # слоты уцелели вместе с инцидентом


async def test_удаление_инцидента_каскадом_убирает_его_слоты_и_доставки(db: Db) -> None:
    old = await db.incidents.open(db.target.id, CUTOFF - timedelta(days=1), slots(1, 2, 3))
    await db.incidents.close(old.id, CUTOFF - timedelta(days=1) + timedelta(minutes=1))
    fresh = await db.incidents.open(db.target.id, CUTOFF + timedelta(days=1), slots(7))
    for incident in (old, fresh):
        await db.incidents.record_delivery(
            DeliveryResult(Channel.TELEGRAM, True, 1.0, 1, "", incident.id), T0
        )
    await db.maintenance.delete_older_than({"incidents": CUTOFF})
    assert await count(db, "incidents") == 1
    assert await count(db, "incident_slots") == 1
    assert await count(db, "deliveries") == 1
    assert await db.incidents.deliveries_for(old.id) == []
    assert len(await db.incidents.deliveries_for(fresh.id)) == 1


async def test_старые_доставки_без_инцидента_тоже_чистятся_по_сроку_инцидентов(db: Db) -> None:
    """SPEC 2.7: «Инциденты и доставки — 180 дней». Служебные сообщения (сбой, зависание) не
    привязаны к инциденту, но это те же доставки: без очистки они копились бы вечно."""
    old, fresh = CUTOFF - timedelta(days=1), CUTOFF + timedelta(days=1)
    for moment in (old, fresh):
        await db.incidents.record_delivery(DeliveryResult(Channel.TELEGRAM, True, 1.0, 1), moment)
    removed = await db.maintenance.delete_older_than({"incidents": CUTOFF})
    assert removed == {"incidents": 0}  # счётчик — про инциденты
    assert await count(db, "deliveries") == 1


async def test_несколько_таблиц_за_один_вызов_и_счётчики_по_таблицам(db: Db) -> None:
    await db.events.write(AppEvent(CUTOFF - timedelta(days=1), "INFO", "a", "m"))
    await db.events.write(AppEvent(CUTOFF - timedelta(days=2), "INFO", "b", "m"))
    await db.stats.save_hour(PollStatsHour(CUTOFF - timedelta(days=1), 1, 0, 0, [1.0]))
    removed = await db.maintenance.delete_older_than(
        {"app_events": CUTOFF, "poll_stats_hourly": CUTOFF, "incidents": CUTOFF}
    )
    assert removed == {"app_events": 2, "poll_stats_hourly": 1, "incidents": 0}


async def test_разные_границы_для_разных_таблиц(db: Db) -> None:
    now = datetime(2026, 11, 12, tzinfo=UTC)
    await db.events.write(AppEvent(now - timedelta(days=40), "INFO", "old-event", "m"))
    await db.stats.save_hour(PollStatsHour(now - timedelta(days=40), 1, 0, 0, [1.0]))
    removed = await db.maintenance.delete_older_than(
        {"app_events": now - timedelta(days=30), "poll_stats_hourly": now - timedelta(days=60)}
    )
    assert removed == {"app_events": 1, "poll_stats_hourly": 0}


async def test_пустой_словарь_границ_ничего_не_удаляет(db: Db) -> None:
    await db.events.write(AppEvent(CUTOFF - timedelta(days=100), "INFO", "a", "m"))
    assert await db.maintenance.delete_older_than({}) == {}
    assert await count(db, "app_events") == 1


async def test_неизвестная_таблица_отвергается_и_ничего_не_удаляется(db: Db) -> None:
    await db.events.write(AppEvent(CUTOFF - timedelta(days=1), "INFO", "a", "m"))
    # Порядок ключей: сначала известная таблица, затем неизвестная — откат должен быть общим
    with pytest.raises(KeyError):
        await db.maintenance.delete_older_than({"app_events": CUTOFF, "kv_settings": CUTOFF})
    assert await count(db, "app_events") == 1
    assert await db.kv.all() == {}


async def test_compact_на_базе_с_wal_уменьшает_файл_и_обнуляет_wal(db: Db, paths: Paths) -> None:
    padding = "я" * 20_000
    for n in range(60):
        await db.events.write(AppEvent(CUTOFF - timedelta(days=1, seconds=n), "INFO", "x", padding))
    await db.maintenance.compact()  # переносит накопленное из WAL в основной файл
    full_size = paths.database.stat().st_size
    assert full_size > 1_000_000

    await db.maintenance.delete_older_than({"app_events": CUTOFF})
    await db.maintenance.compact()

    # В режиме WAL VACUUM пишет результат в WAL: пока его не перенесли в основной файл,
    # файл на диске остаётся прежнего размера и «уплотнение» ничего не освобождает
    assert paths.database.stat().st_size < full_size / 10
    wal = paths.database.with_name(paths.database.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    # Данные, которые должны были остаться, на месте
    await db.kv.set("после", "уплотнения")
    assert await db.kv.get("после") == "уплотнения"


async def test_compact_не_теряет_данные_и_повторяется_безопасно(db: Db) -> None:
    await db.events.write(AppEvent(T0, "ERROR", "keep", "оставить"))
    incident = await db.incidents.open(db.target.id, T0, slots(5, 6))
    for _ in range(3):
        await db.maintenance.compact()
    assert [e.code for e in await db.events.last(5)] == ["keep"]
    current = await db.incidents.current(db.target.id)
    assert current is not None and current.id == incident.id and current.slots == slots(5, 6)


async def test_compact_на_пустой_базе_не_падает(db: Db) -> None:
    await db.maintenance.compact()
