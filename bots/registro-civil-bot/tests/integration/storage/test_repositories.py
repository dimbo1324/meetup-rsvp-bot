"""Репозитории SQLAlchemy на настоящей файловой SQLite (SPEC 3.2): круг записи и чтения."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select, text

from rcbot.domain.models import (
    AppEvent,
    Channel,
    DeliveryResult,
    IncidentStatus,
    PollStatsHour,
    Slot,
)
from rcbot.infrastructure.db.tables import IncidentSlotRow
from tests.fakes import T0, slots, slots_at
from tests.integration.storage.conftest import Db

# --- цели наблюдения ---


async def test_ensure_создаёт_цель_и_возвращает_все_поля(db: Db) -> None:
    target = await db.targets.ensure("другая", "https://x.test/a", "Офис", 3)
    assert (target.name, target.agenda_url, target.location_text, target.months_ahead) == (
        "другая",
        "https://x.test/a",
        "Офис",
        3,
    )
    assert target.id != db.target.id


async def test_в_бд_не_попадает_номер_этапа_заявки(db: Db) -> None:
    # Инвариант 6 и решение R1: номер этапа — только в настройках, в БД остаётся хост.
    # Вызывающий при этом получает полный адрес обратно: источникам он нужен.
    url = "https://bpmgob.mec.gub.uy/etapas/ejecutar/TEST-ETAPA-777/1"
    target = await db.targets.ensure("registro-civil", url, "Офис", 6)
    assert target.agenda_url == url
    async with db.engine.connect() as conn:
        stored = (await conn.execute(text("SELECT agenda_url FROM watch_targets"))).scalars().all()
    assert stored == ["https://bpmgob.mec.gub.uy/…"]
    assert all("TEST-ETAPA" not in value and "/etapas" not in value for value in stored)


async def test_ensure_идемпотентен_и_обновляет_поля(db: Db) -> None:
    again = await db.targets.ensure("registro-civil", "https://new.test/b", "Новый офис", 2)
    assert again.id == db.target.id
    assert (again.agenda_url, again.location_text, again.months_ahead) == (
        "https://new.test/b",
        "Новый офис",
        2,
    )
    # Строка одна: повторный вызов не плодит дубли
    async with db.engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM watch_targets"))).scalar() == 1


# --- инциденты ---


async def test_открытый_инцидент_читается_как_текущий_со_слотами(db: Db) -> None:
    found = slots(23, 24)
    opened = await db.incidents.open(db.target.id, T0, found)
    current = await db.incidents.current(db.target.id)
    assert current is not None
    assert (current.id, current.opened_at, current.slots) == (opened.id, T0, found)
    assert current.status is IncidentStatus.OPEN
    assert current.closed_at is None and current.acked_at is None and not current.acked


async def test_слоты_со_временем_и_без_сохраняются_точно(db: Db) -> None:
    with_time = slots_at((23, 10, 30), (23, 10, 45), (24, 9, 0))
    opened = await db.incidents.open(db.target.id, T0, with_time)
    current = await db.incidents.current(db.target.id)
    assert current is not None and current.slots == with_time
    assert opened.slots == with_time

    only_days = await db.incidents.open(db.target.id, T0 + timedelta(hours=1), slots(1, 2))
    assert only_days.slots == slots(1, 2)
    assert all(slot.at is None for slot in (await db.incidents.last(1))[0].slots)


async def test_в_одном_инциденте_день_без_времени_и_тот_же_день_со_временем(db: Db) -> None:
    mixed = frozenset({Slot(date(2027, 2, 23)), Slot(date(2027, 2, 23), time(10, 0))})
    opened = await db.incidents.open(db.target.id, T0, mixed)
    assert opened.slots == mixed
    current = await db.incidents.current(db.target.id)
    assert current is not None and current.slots == mixed


async def test_update_slots_заменяет_набор_а_не_дополняет(db: Db) -> None:
    incident = await db.incidents.open(db.target.id, T0, slots(23, 24))
    await db.incidents.update_slots(incident.id, slots(24, 25, 26))
    current = await db.incidents.current(db.target.id)
    assert current is not None and current.slots == slots(24, 25, 26)
    async with db.sessions() as session:
        rows = (await session.scalars(select(IncidentSlotRow))).all()
    assert len(rows) == 3  # старые строки удалены, а не накоплены


async def test_update_slots_пустым_набором_очищает_слоты(db: Db) -> None:
    incident = await db.incidents.open(db.target.id, T0, slots(23))
    await db.incidents.update_slots(incident.id, frozenset())
    current = await db.incidents.current(db.target.id)
    assert current is not None and current.slots == frozenset()


async def test_close_ставит_статус_и_время_и_инцидент_больше_не_текущий(db: Db) -> None:
    incident = await db.incidents.open(db.target.id, T0, slots(23))
    closed_at = T0 + timedelta(minutes=7)
    await db.incidents.close(incident.id, closed_at)
    assert await db.incidents.current(db.target.id) is None
    (last,) = await db.incidents.last(5)
    assert last.status is IncidentStatus.CLOSED
    assert last.closed_at == closed_at
    assert last.slots == slots(23)  # слоты закрытого инцидента сохраняются для истории


async def test_ack_записывается_один_раз_и_не_закрывает_инцидент(db: Db) -> None:
    incident = await db.incidents.open(db.target.id, T0, slots(23))
    first, second = T0 + timedelta(seconds=30), T0 + timedelta(minutes=5)
    await db.incidents.ack(incident.id, first)
    await db.incidents.ack(incident.id, second)
    current = await db.incidents.current(db.target.id)
    assert current is not None
    assert current.acked_at == first  # «Вижу» повторно не перезаписывает момент
    assert current.status is IncidentStatus.OPEN  # «Вижу» останавливает повторы, не инцидент


async def test_ack_несуществующего_инцидента_ничего_не_ломает(db: Db) -> None:
    await db.incidents.ack(12345, T0)
    assert await db.incidents.last(5) == []


async def test_current_возвращает_самый_новый_открытый_инцидент_своей_цели(db: Db) -> None:
    other = await db.targets.ensure("вторая", "https://y.test/", "Офис 2", 1)
    older = await db.incidents.open(db.target.id, T0, slots(1))
    newer = await db.incidents.open(db.target.id, T0 + timedelta(hours=1), slots(2))
    await db.incidents.open(other.id, T0 + timedelta(hours=2), slots(3))
    current = await db.incidents.current(db.target.id)
    assert current is not None and current.id == newer.id != older.id
    assert (await db.incidents.current(other.id)) is not None
    assert await db.incidents.current(999) is None


async def test_last_отдаёт_новые_первыми_и_соблюдает_лимит(db: Db) -> None:
    ids = [
        (await db.incidents.open(db.target.id, T0 + timedelta(hours=n), slots(n + 1))).id
        for n in range(5)
    ]
    got = await db.incidents.last(3)
    assert [i.id for i in got] == ids[::-1][:3]
    assert [i.slots for i in got] == [slots(5), slots(4), slots(3)]
    assert await db.incidents.last(0) == []


async def test_last_на_пустой_базе_пустой_список(db: Db) -> None:
    assert await db.incidents.last(10) == []


# --- доставки ---


async def test_доставки_возвращаются_по_порядку_записи_только_своего_инцидента(db: Db) -> None:
    one = await db.incidents.open(db.target.id, T0, slots(1))
    two = await db.incidents.open(db.target.id, T0, slots(2))
    results = [
        DeliveryResult(Channel.TELEGRAM, True, 12.5, 1, "", one.id),
        DeliveryResult(Channel.NTFY, False, 3000.0, 2, "таймаут", one.id),
        DeliveryResult(Channel.CALL, True, 800.0, 1, "", two.id),
    ]
    for result in results:
        await db.incidents.record_delivery(result, T0)
    assert await db.incidents.deliveries_for(one.id) == results[:2]
    assert await db.incidents.deliveries_for(two.id) == results[2:]
    assert await db.incidents.deliveries_for(999) == []


async def test_текст_ошибки_доставки_усекается_до_300_символов(db: Db) -> None:
    incident = await db.incidents.open(db.target.id, T0, slots(1))
    long_error = "ошибка " * 100
    await db.incidents.record_delivery(
        DeliveryResult(Channel.NTFY, False, 1.0, 1, long_error, incident.id), T0
    )
    (stored,) = await db.incidents.deliveries_for(incident.id)
    assert stored.error == long_error[:300]
    assert len(stored.error) == 300


async def test_доставка_без_инцидента_служебное_сообщение_записывается(db: Db) -> None:
    await db.incidents.record_delivery(DeliveryResult(Channel.TELEGRAM, True, 1.0, 1), T0)
    async with db.engine.connect() as conn:
        row = (await conn.execute(text("SELECT incident_id, ok, at FROM deliveries"))).one()
    assert row[0] is None and row[1] == 1
    assert row[2] == "2026-11-12T15:00:00.000000+00:00"


# --- статистика ---


async def test_save_hour_пишет_строку_и_перезаписывает_тот_же_час(db: Db) -> None:
    hour = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
    first = PollStatsHour(hour, checks=10, errors=1, relogins=0, latencies_ms=[10.0, 20.0, 30.0])
    await db.stats.save_hour(first)
    second = PollStatsHour(hour, checks=25, errors=2, relogins=1, latencies_ms=[5.0])
    await db.stats.save_hour(second)
    (stored,) = await db.stats.since(hour)
    assert (stored.hour, stored.checks, stored.errors, stored.relogins) == (hour, 25, 2, 1)
    assert stored.percentile(0.5) == 5.0


async def test_перцентили_считаются_при_записи_и_переживают_чтение(db: Db) -> None:
    hour = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
    latencies = [float(n) for n in range(1, 101)]
    original = PollStatsHour(hour, 100, 0, 0, latencies)
    await db.stats.save_hour(original)
    (stored,) = await db.stats.since(hour)
    assert stored.percentile(0.50) == original.percentile(0.50)
    assert stored.percentile(0.95) == original.percentile(0.95)


async def test_перцентили_совпадают_для_одной_и_двух_точек_и_пустого_часа(db: Db) -> None:
    base = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
    cases = {
        base: [42.0],
        base + timedelta(hours=1): [10.0, 90.0],
        base + timedelta(hours=2): [],
    }
    for hour, values in cases.items():
        await db.stats.save_hour(PollStatsHour(hour, len(values), 0, 0, values))
    stored = {h.hour: h for h in await db.stats.since(base)}
    for hour, values in cases.items():
        expected = PollStatsHour(hour, 0, 0, 0, values)
        assert stored[hour].percentile(0.50) == expected.percentile(0.50), values
        assert stored[hour].percentile(0.95) == expected.percentile(0.95), values


async def test_since_возвращает_часы_не_раньше_границы_по_порядку(db: Db) -> None:
    base = datetime(2026, 11, 12, 0, 0, tzinfo=UTC)
    for hours in (5, 1, 3, 2, 4):  # записываем вразнобой
        await db.stats.save_hour(PollStatsHour(base + timedelta(hours=hours), hours, 0, 0, [1.0]))
    got = await db.stats.since(base + timedelta(hours=3))
    assert [h.checks for h in got] == [3, 4, 5]  # граница включается
    assert await db.stats.since(base + timedelta(hours=6)) == []


# --- журнал событий ---


async def test_события_фильтруются_по_уровню_и_идут_новыми_первыми(db: Db) -> None:
    levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    for n, level in enumerate(levels):
        await db.events.write(AppEvent(T0 + timedelta(seconds=n), level, f"c{n}", f"m{n}"))
    assert [e.level for e in await db.events.last(10)] == ["CRITICAL", "ERROR", "WARNING"]
    assert [e.level for e in await db.events.last(10, "ERROR")] == ["CRITICAL", "ERROR"]
    assert [e.level for e in await db.events.last(10, "DEBUG")] == levels[::-1]
    assert [e.code for e in await db.events.last(2, "INFO")] == ["c4", "c3"]


async def test_событие_читается_со_всеми_полями(db: Db) -> None:
    event = AppEvent(T0, "WARNING", "poll.rate_limited", "429 от сайта")
    await db.events.write(event)
    assert await db.events.last(5) == [event]


async def test_неизвестный_уровень_в_запросе_трактуется_как_warning(db: Db) -> None:
    await db.events.write(AppEvent(T0, "INFO", "a", "info"))
    await db.events.write(AppEvent(T0, "ERROR", "b", "error"))
    assert [e.code for e in await db.events.last(5, "ВЫДУМАННЫЙ")] == ["b"]


# --- настройки ---


async def test_kv_get_set_all(db: Db) -> None:
    assert await db.kv.get("interval") is None
    await db.kv.set("interval", "20")
    await db.kv.set("channels", "telegram,ntfy")
    await db.kv.set("interval", "30")  # перезапись, а не вторая строка
    assert await db.kv.get("interval") == "30"
    assert await db.kv.all() == {"interval": "30", "channels": "telegram,ntfy"}


async def test_kv_хранит_юникод_и_пустую_строку(db: Db) -> None:
    await db.kv.set("пусто", "")
    await db.kv.set("текст", "Монтевидео — ñ")
    assert await db.kv.get("пусто") == ""
    assert await db.kv.get("текст") == "Монтевидео — ñ"
