"""Запись и отмена: дополнительные ветки (5.3, 5.4), уведомления и конкурентность."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select

from meetup_bot.db.models import EventStatus, Registration, User
from meetup_bot.repositories import events
from meetup_bot.services.registration import CancelStatus, RegisterStatus
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

# Моменты ближайшей встречи при часах «пятница 12:00 UTC»
# От «сейчас» (четверг 12:00 UTC) до моментов встречи по умолчанию (пятница 16:30 в Монтевидео)
STARTS_IN = timedelta(hours=31, minutes=30)  # пятница 19:30 UTC
DEADLINE_HOURS = 11  # четверг 23:00 UTC
CLOSES_IN = timedelta(hours=32, minutes=30)  # пятница 20:30 UTC: через час после начала


async def make_event(app: App, capacity: int = 10) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)).event_id


async def enable(app: App, *keys: str) -> None:
    for key in keys:
        await app.services.settings.set(key, True, OWNER_ID)


async def taken(app: App, event_id: int) -> int:
    async with app.sessions() as session:
        return await events.seats_taken(session, event_id)


async def status_of(app: App, event_id: int, user_id: int) -> str:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration.status).where(
                Registration.event_id == event_id, Registration.user_id == user_id
            )
        )
        assert found is not None
        return found


# ---- результат записи ----


async def test_результат_записи_содержит_данные_встречи(app: App) -> None:
    await app.services.settings.set("default_place", "Кафе", OWNER_ID)
    event_id = (
        await app.services.events.create_event_as_admin(OWNER_ID, capacity=5, place="Кафе")
    ).event_id
    result = await app.services.registration.register(event_id, person(1))
    assert result.starts_at == app.clock.now() + STARTS_IN
    assert result.cancel_deadline_at == app.clock.now() + timedelta(hours=DEADLINE_HOURS)
    assert result.place == "Кафе"
    assert (result.seats_taken, result.capacity) == (1, 5)


async def test_dm_available_ложь_для_нового_пользователя(app: App) -> None:
    event_id = await make_event(app)
    assert (await app.services.registration.register(event_id, person(1))).dm_available is False


async def test_dm_available_истина_если_пользователь_писал_боту(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    async with app.sessions() as session, session.begin():
        user = await session.get(User, 1001)
        assert user is not None
        user.dm_available = True
    await app.services.registration.cancel(event_id, 1001)
    again = await app.services.registration.register(event_id, person(1))
    assert again.dm_available is True


async def test_результат_для_несуществующей_встречи(app: App) -> None:
    result = await app.services.registration.register(404, person(1))
    assert result.status is RegisterStatus.NOT_FOUND
    assert result.changed_state is False


async def test_состояние_меняют_только_ok_и_waitlisted(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    reg = app.services.registration
    first = await reg.register(event_id, person(1))
    queued = await reg.register(event_id, person(2))
    again = await reg.register(event_id, person(1))
    assert (first.changed_state, queued.changed_state, again.changed_state) == (True, True, False)


async def test_запись_пересобирает_пост_а_отказ_нет(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", False, OWNER_ID)  # отказ = FULL
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(1))
    assert app.refresher.scheduled.count(event_id) == 1
    await app.services.registration.register(event_id, person(2))
    assert app.refresher.scheduled.count(event_id) == 1


# ---- очередь ----


async def test_повторная_запись_из_очереди_даёт_already_waitlist_с_позицией(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    reg = app.services.registration
    await reg.register(event_id, person(1))
    await reg.register(event_id, person(2))
    await reg.register(event_id, person(3))
    again = await reg.register(event_id, person(3))
    assert (again.status, again.position) == (RegisterStatus.ALREADY_WAITLIST, 2)


async def test_позиции_в_очереди_идут_подряд(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    reg = app.services.registration
    await reg.register(event_id, person(0))
    positions = [(await reg.register(event_id, person(n))).position for n in range(1, 4)]
    assert positions == [1, 2, 3]


async def test_выход_из_очереди_сдвигает_позиции_остальных(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    reg = app.services.registration
    for n in range(4):
        await reg.register(event_id, person(n))
    await reg.cancel(event_id, 1001)
    assert (await reg.register(event_id, person(3))).position == 2


async def test_выход_из_очереди_без_штрафа_и_без_уведомления_админам(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    reg = app.services.registration
    await reg.register(event_id, person(1))
    await reg.register(event_id, person(2))
    app.notifier.service.clear()
    left = await reg.cancel(event_id, 1002)
    assert (left.status, left.was_waitlist) == (CancelStatus.OK, True)
    assert app.notifier.service == []
    assert await status_of(app, event_id, 1002) == "cancelled"


async def test_из_очереди_можно_выйти_после_дедлайна_отмены_но_до_закрытия(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    reg = app.services.registration
    await reg.register(event_id, person(1))
    await reg.register(event_id, person(2))
    app.clock.advance(timedelta(hours=DEADLINE_HOURS, minutes=30))
    assert (await reg.cancel(event_id, 1002)).status is CancelStatus.OK


async def test_после_закрытия_записи_из_очереди_выйти_нельзя(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    reg = app.services.registration
    await reg.register(event_id, person(1))
    await reg.register(event_id, person(2))
    app.clock.advance(CLOSES_IN + timedelta(minutes=1))
    result = await reg.cancel(event_id, 1002)
    assert (result.status, result.was_waitlist) == (CancelStatus.TOO_LATE, True)
    assert await status_of(app, event_id, 1002) == "waitlist"


# ---- отмена ----


async def test_отмена_после_закрытия_записи_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    app.clock.advance(CLOSES_IN + timedelta(minutes=1))
    result = await app.services.registration.cancel(event_id, 1001)
    assert result.status is CancelStatus.TOO_LATE
    assert await taken(app, event_id) == 1


async def test_отмена_у_закрытой_админом_встречи_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    result = await app.services.registration.cancel(event_id, 1001)
    assert result.status is CancelStatus.TOO_LATE


async def test_отмена_возвращает_данные_встречи_и_занятые_места(app: App) -> None:
    event_id = await make_event(app, 5)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    result = await app.services.registration.cancel(event_id, 1001)
    assert (result.seats_taken, result.capacity) == (1, 5)
    assert result.starts_at == app.clock.now() + STARTS_IN
    assert result.changed_state is True


async def test_отмена_без_записи_ничего_не_меняет(app: App) -> None:
    event_id = await make_event(app)
    app.refresher.scheduled.clear()
    result = await app.services.registration.cancel(event_id, 1001)
    assert (result.status, result.changed_state) == (CancelStatus.NOT_REGISTERED, False)
    assert app.refresher.scheduled == []


async def test_запись_после_отмены_создаёт_новую_запись(app: App) -> None:
    event_id = await make_event(app)
    reg = app.services.registration
    await reg.register(event_id, person(1))
    await reg.cancel(event_id, 1001)
    assert (await reg.register(event_id, person(1))).status is RegisterStatus.OK
    async with app.sessions() as session:
        rows = (await session.scalars(select(Registration.status))).all()
    assert sorted(rows) == ["active", "cancelled"]


# ---- закрытие и дедлайн ----


async def test_закрытая_админом_встреча_не_принимает_запись(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.CLOSED


async def test_запись_за_секунду_до_закрытия_проходит(app: App) -> None:
    event_id = await make_event(app)
    app.clock.advance(CLOSES_IN - timedelta(seconds=1))
    result = await app.services.registration.register(event_id, person(1))
    assert (result.status, result.cancel_deadline_passed) == (RegisterStatus.OK, True)


async def test_до_дедлайна_флаг_позднего_дедлайна_не_ставится(app: App) -> None:
    event_id = await make_event(app)
    result = await app.services.registration.register(event_id, person(1))
    assert result.cancel_deadline_passed is False


async def test_запись_в_момент_дедлайна_уже_помечается_поздней(app: App) -> None:
    event_id = await make_event(app)
    app.clock.advance(timedelta(hours=DEADLINE_HOURS))
    assert (await app.services.registration.register(event_id, person(1))).cancel_deadline_passed


async def test_запись_на_отменённую_встречу_закрыта(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id, "дождь")
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.CLOSED


# ---- заполнение и уведомления ----


async def test_последняя_запись_шлёт_служебное_уведомление_о_заполнении(app: App) -> None:
    event_id = await make_event(app, 2)
    await app.services.registration.register(event_id, person(1))
    assert app.notifier.service == []
    await app.services.registration.register(event_id, person(2))
    assert len(app.notifier.service) == 1
    assert "2 из 2" in app.notifier.service[0]


async def test_уведомление_о_заполнении_отключается_настройкой(app: App) -> None:
    await app.services.settings.set("notify_admins_on_full", False, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(1))
    assert app.notifier.service == []


async def test_плюс_один_заполнивший_встречу_ставит_filled_event(app: App) -> None:
    await enable(app, "plus_one_enabled")
    event_id = await make_event(app, 2)
    result = await app.services.registration.register(event_id, person(1), guests=1)
    assert result.filled_event is True


async def test_отмена_шлёт_служебное_уведомление_по_умолчанию(app: App) -> None:
    event_id = await make_event(app, 5)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.cancel(event_id, 1001)
    assert len(app.notifier.service) == 1
    assert "Участник1" in app.notifier.service[0]


async def test_уведомление_об_отмене_отключается_настройкой(app: App) -> None:
    await app.services.settings.set("notify_admins_on_cancel", False, OWNER_ID)
    event_id = await make_event(app, 5)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.cancel(event_id, 1001)
    assert app.notifier.service == []


async def test_сбой_служебного_уведомления_не_ломает_запись(app: App) -> None:
    async def broken(text: str) -> None:
        raise RuntimeError("telegram недоступен")

    app.notifier.send_service = broken  # type: ignore[method-assign]
    event_id = await make_event(app, 1)
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.OK
    assert await taken(app, event_id) == 1


async def test_продвинутому_с_заблокированным_ботом_лс_не_доставляется(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    reg = app.services.registration
    await reg.register(event_id, person(1))
    await reg.register(event_id, person(2))
    app.notifier.blocked.add(1002)
    result = await reg.cancel(event_id, 1001)
    assert [p.user_id for p in result.promoted] == [1002]
    assert app.notifier.dms_to(1002) == []
    assert await status_of(app, event_id, 1002) == "active"


async def test_закрытие_записи_статусом_closed_не_меняет_места(app: App) -> None:
    event_id = await make_event(app, 3)
    await app.services.registration.register(event_id, person(1))
    async with app.sessions() as session:
        event = await events.get(session, event_id)
        assert event is not None
        assert event.status == EventStatus.OPEN.value


# ---- конкурентность ----


async def test_30_параллельных_записей_на_10_мест_через_приложение(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", False, OWNER_ID)
    event_id = await make_event(app, 10)
    reg = app.services.registration
    results = await asyncio.gather(*(reg.register(event_id, person(n)) for n in range(30)))
    statuses = [r.status for r in results]
    assert statuses.count(RegisterStatus.OK) == 10
    assert statuses.count(RegisterStatus.FULL) == 20
    assert await taken(app, event_id) == 10


async def test_ровно_одна_запись_помечена_как_заполнившая(app: App) -> None:
    event_id = await make_event(app, 10)
    reg = app.services.registration
    results = await asyncio.gather(*(reg.register(event_id, person(n)) for n in range(30)))
    assert sum(r.filled_event for r in results) == 1


async def test_параллельная_запись_и_удаление_админом_не_нарушают_лимит(app: App) -> None:
    event_id = await make_event(app, 5)
    reg = app.services.registration
    for n in range(5):
        await reg.register(event_id, person(n))
    async with app.sessions() as session:
        ids = list(
            (
                await session.scalars(
                    select(Registration.id).where(Registration.event_id == event_id)
                )
            ).all()
        )
    removals = [app.services.participants.admin_remove(ADMIN_ID, event_id, rid) for rid in ids[:2]]
    signups = [reg.register(event_id, person(n)) for n in range(10, 30)]
    await asyncio.gather(*removals, *signups)
    assert await taken(app, event_id) == 5


async def test_параллельные_записи_и_очередь_не_превышают_лимит(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 4)
    reg = app.services.registration
    results = await asyncio.gather(*(reg.register(event_id, person(n)) for n in range(20)))
    assert [r.status for r in results].count(RegisterStatus.OK) == 4
    assert [r.status for r in results].count(RegisterStatus.WAITLISTED) == 16
    positions = sorted(r.position for r in results if r.position is not None)
    assert positions == list(range(1, 17))


async def test_параллельные_отмены_продвигают_каждого_один_раз(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 3)
    reg = app.services.registration
    for n in range(6):
        await reg.register(event_id, person(n))
    await asyncio.gather(*(reg.cancel(event_id, 1000 + n) for n in range(3)))
    assert await taken(app, event_id) == 3
    for n in (3, 4, 5):
        assert await status_of(app, event_id, 1000 + n) == "active"


async def test_мои_записи_пусты_без_записей(app: App) -> None:
    assert await app.services.registration.my_registrations(77) == []


async def test_мои_записи_показывают_запись_гостя_и_очередь(app: App) -> None:
    first = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=1)).event_id
    second = (
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=app.clock.now() + timedelta(days=8)
        )
    ).event_id
    await app.services.registration.register(first, person(1))
    await app.services.registration.register(first, person(2))
    await app.services.registration.register(second, person(2))
    mine = await app.services.registration.my_registrations(person(2).id)
    assert [item.waitlist_position for item in mine] == [1, None]
    assert mine[0].starts_at < mine[1].starts_at
    assert all(item.ends_at > item.starts_at for item in mine)


async def test_мои_записи_не_включают_отменённую_запись_и_чужие(app: App) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID)).event_id
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    await app.services.registration.cancel(event_id, person(1).id)
    assert await app.services.registration.my_registrations(person(1).id) == []
    assert len(await app.services.registration.my_registrations(person(2).id)) == 1
