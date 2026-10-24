"""Лист ожидания и продвижение очереди (5.5), лимит мест и «+1» (кейсы 11–13)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from meetup_bot.db.models import Ban, BanReason, EventStatus, Registration
from meetup_bot.repositories import events
from meetup_bot.services.errors import InvalidOperation
from meetup_bot.services.registration import CancelStatus, RegisterStatus
from meetup_bot.services.waitlist import promote_waitlist
from tests.helpers import ADMIN_ID, OWNER_ID, App, person


async def make_event(app: App, capacity: int) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)).event_id


async def enable(app: App, *keys: str) -> None:
    for key in keys:
        await app.services.settings.set(key, True, OWNER_ID)


async def statuses(app: App, event_id: int) -> dict[int, str]:
    """user_id -> статус записи (для людей с одной записью)."""
    async with app.sessions() as session:
        rows = await session.execute(
            select(Registration.user_id, Registration.status).where(
                Registration.event_id == event_id
            )
        )
        return {user_id: status for user_id, status in rows.all()}


async def reg_of(app: App, event_id: int, user_id: int) -> Registration:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration).where(
                Registration.event_id == event_id, Registration.user_id == user_id
            )
        )
        assert found is not None
        return found


async def taken(app: App, event_id: int) -> int:
    async with app.sessions() as session:
        return await events.seats_taken(session, event_id)


async def fill_and_queue(app: App, event_id: int, active: int, queued: int) -> None:
    """Записывает active+queued человек (id 1000+n) по порядку: лишние уходят в очередь."""
    for n in range(active + queued):
        await app.services.registration.register(event_id, person(n))


# ---- кейс 11: лимит мест ----


async def test_лимит_ниже_занятых_мест_отклоняется(app: App) -> None:  # кейс 11
    event_id = await make_event(app, 5)
    for n in range(4):
        await app.services.registration.register(event_id, person(n))
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.set_capacity(OWNER_ID, event_id, 3)
    assert exc.value.code == "capacity_below_taken"
    async with app.sessions() as session:
        event = await events.get(session, event_id)
        assert event is not None
        assert event.capacity == 5


async def test_лимит_равный_занятым_местам_допустим(app: App) -> None:
    event_id = await make_event(app, 5)
    for n in range(4):
        await app.services.registration.register(event_id, person(n))
    await app.services.events.set_capacity(OWNER_ID, event_id, 4)
    result = await app.services.registration.register(event_id, person(9))
    assert result.status is RegisterStatus.FULL


async def test_лимит_учитывает_гостей_при_проверке_занятых(app: App) -> None:
    await enable(app, "plus_one_enabled")
    event_id = await make_event(app, 5)
    await app.services.registration.register(event_id, person(1), guests=1)
    await app.services.registration.register(event_id, person(2), guests=1)
    with pytest.raises(InvalidOperation):
        await app.services.events.set_capacity(OWNER_ID, event_id, 3)


@pytest.mark.parametrize("bad", [0, -1, 201, 1000])
async def test_лимит_вне_диапазона_1_200(app: App, bad: int) -> None:
    event_id = await make_event(app, 5)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.set_capacity(OWNER_ID, event_id, bad)
    assert exc.value.code == "invalid_capacity"


@pytest.mark.parametrize("edge", [1, 200])
async def test_границы_лимита_допустимы(app: App, edge: int) -> None:
    event_id = await make_event(app, 5)
    await app.services.events.set_capacity(OWNER_ID, event_id, edge)
    async with app.sessions() as session:
        event = await events.get(session, event_id)
        assert event is not None
        assert event.capacity == edge


# ---- кейс 12: продвижение ----


async def test_увеличение_лимита_продвигает_очередь_по_порядку(app: App) -> None:  # кейс 12
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 2)
    await fill_and_queue(app, event_id, 2, 3)
    await app.services.events.set_capacity(OWNER_ID, event_id, 4)
    state = await statuses(app, event_id)
    assert [state[1000 + n] for n in range(5)] == ["active"] * 4 + ["waitlist"]
    assert await taken(app, event_id) == 4


async def test_продвинутый_получает_время_продвижения(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 1)
    await app.services.events.set_capacity(OWNER_ID, event_id, 2)
    assert (await reg_of(app, event_id, 1001)).promoted_at == app.clock.now()
    assert (await reg_of(app, event_id, 1000)).promoted_at is None


async def test_плюс_один_без_двух_мест_пропускается_и_остаётся_в_очереди(app: App) -> None:
    await enable(app, "waitlist_enabled", "plus_one_enabled")
    event_id = await make_event(app, 2)
    reg = app.services.registration
    await reg.register(event_id, person(0))
    await reg.register(event_id, person(1))
    assert (await reg.register(event_id, person(2), guests=1)).position == 1
    assert (await reg.register(event_id, person(3))).position == 2
    await reg.cancel(event_id, 1000)
    state = await statuses(app, event_id)
    assert state[1002] == "waitlist"
    assert state[1003] == "active"
    # «+1» сохранил свою позицию — он по-прежнему первый в очереди
    again = await reg.register(event_id, person(2), guests=1)
    assert (again.status, again.position) == (RegisterStatus.ALREADY_WAITLIST, 1)


async def test_плюс_один_продвигается_когда_появилось_два_места(app: App) -> None:
    await enable(app, "waitlist_enabled", "plus_one_enabled")
    event_id = await make_event(app, 2)
    reg = app.services.registration
    await reg.register(event_id, person(0))
    await reg.register(event_id, person(1))
    await reg.register(event_id, person(2), guests=1)
    await reg.cancel(event_id, 1000)
    await reg.cancel(event_id, 1001)
    assert (await statuses(app, event_id))[1002] == "active"
    assert await taken(app, event_id) == 2


async def test_продвижение_не_превышает_лимит(app: App) -> None:
    await enable(app, "waitlist_enabled", "plus_one_enabled")
    event_id = await make_event(app, 3)
    reg = app.services.registration
    for n in range(3):
        await reg.register(event_id, person(n))
    for n in range(3, 7):
        await reg.register(event_id, person(n), guests=n % 2)
    await app.services.events.set_capacity(OWNER_ID, event_id, 6)
    assert await taken(app, event_id) <= 6


async def test_забаненный_в_очереди_пропускается(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 2)
    async with app.sessions() as session, session.begin():
        session.add(Ban(user_id=1001, reason=BanReason.MANUAL.value, banned_at=app.clock.now()))
    await app.services.registration.cancel(event_id, 1000)
    state = await statuses(app, event_id)
    assert state[1001] == "waitlist"
    assert state[1002] == "active"


async def test_отмена_активной_записи_продвигает_первого_и_шлёт_лс(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 2)
    result = await app.services.registration.cancel(event_id, 1000)
    assert result.status is CancelStatus.OK
    assert [p.user_id for p in result.promoted] == [1001]
    assert len(app.notifier.dms_to(1001)) == 1
    assert app.notifier.dms_to(1002) == []
    assert (await statuses(app, event_id))[1002] == "waitlist"


async def test_отмена_пересобирает_пост(app: App) -> None:
    event_id = await make_event(app, 2)
    await app.services.registration.register(event_id, person(0))
    app.refresher.scheduled.clear()
    await app.services.registration.cancel(event_id, 1000)
    assert app.refresher.scheduled == [event_id]


async def test_admin_remove_продвигает_очередь(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 1)
    active = await reg_of(app, event_id, 1000)
    result = await app.services.participants.admin_remove(ADMIN_ID, event_id, active.id)
    assert [p.user_id for p in result.promoted] == [1001]
    assert (await statuses(app, event_id))[1001] == "active"


async def test_открытие_закрытой_встречи_раздаёт_очередь(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 2)
    await fill_and_queue(app, event_id, 2, 2)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    # пока запись закрыта, админ освободил место вручную — очередь не двигается
    first = await reg_of(app, event_id, 1000)
    await app.services.participants.admin_remove(ADMIN_ID, event_id, first.id)
    assert (await statuses(app, event_id))[1002] == "waitlist"
    await app.services.events.set_registration_open(OWNER_ID, event_id, True)
    assert (await statuses(app, event_id))[1002] == "active"


async def test_повтор_с_тем_же_лимитом_не_продвигает_очередь(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 2)
    await app.services.events.set_capacity(OWNER_ID, event_id, 2)
    before = await statuses(app, event_id)
    await app.services.events.set_capacity(OWNER_ID, event_id, 2)
    assert await statuses(app, event_id) == before
    assert before[1002] == "waitlist"
    assert await taken(app, event_id) == 2


async def test_promote_waitlist_напрямую_идемпотентен(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 2)
    await fill_and_queue(app, event_id, 2, 2)
    async with app.sessions() as session, session.begin():
        event = await events.get(session, event_id)
        assert event is not None
        event.capacity = 3
        first = await promote_waitlist(session, event, app.clock.now())
        second = await promote_waitlist(session, event, app.clock.now())
    assert [p.user_id for p in first] == [1002]
    assert second == []
    assert await taken(app, event_id) == 3


async def test_promote_waitlist_на_закрытой_встрече_ничего_не_делает(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 1)
    async with app.sessions() as session, session.begin():
        event = await events.get(session, event_id)
        assert event is not None
        event.capacity = 2
        event.status = EventStatus.CLOSED.value
        assert await promote_waitlist(session, event, app.clock.now()) == []


async def test_очередь_продвигается_даже_после_выключения_флага(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 1)
    await app.services.settings.set("waitlist_enabled", False, OWNER_ID)
    await app.services.registration.cancel(event_id, 1000)
    assert (await statuses(app, event_id))[1001] == "active"


# ---- late_promoted ----


async def test_раннее_продвижение_не_помечается_поздним(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 1)
    await app.services.registration.cancel(event_id, 1000)
    assert (await reg_of(app, event_id, 1001)).late_promoted is False


async def test_продвижение_ближе_порога_помечается_поздним(app: App) -> None:
    await enable(app, "waitlist_enabled")
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 1)
    # Пятница 23:30 UTC: до начала 20.5 ч < 24 ч, а отмена по дедлайну уже закрыта → через админа
    app.clock.advance(timedelta(hours=11, minutes=30))
    active = await reg_of(app, event_id, 1000)
    await app.services.participants.admin_remove(ADMIN_ID, event_id, active.id)
    assert (await reg_of(app, event_id, 1001)).late_promoted is True


async def test_порог_поздней_подвижки_берётся_из_настройки(app: App) -> None:
    await enable(app, "waitlist_enabled")
    await app.services.settings.set("late_promotion_hours", 72, OWNER_ID)
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 1)
    await app.services.registration.cancel(event_id, 1000)
    assert (await reg_of(app, event_id, 1001)).late_promoted is True


async def test_нулевой_порог_никогда_не_даёт_позднюю_подвижку(app: App) -> None:
    await enable(app, "waitlist_enabled")
    await app.services.settings.set("late_promotion_hours", 0, OWNER_ID)
    event_id = await make_event(app, 1)
    await fill_and_queue(app, event_id, 1, 1)
    app.clock.advance(timedelta(hours=11, minutes=30))
    active = await reg_of(app, event_id, 1000)
    await app.services.participants.admin_remove(ADMIN_ID, event_id, active.id)
    assert (await reg_of(app, event_id, 1001)).late_promoted is False


# ---- кейс 13: «+1» ----


async def test_плюс_один_при_одном_свободном_месте_без_очереди(app: App) -> None:  # кейс 13
    await enable(app, "plus_one_enabled")
    event_id = await make_event(app, 3)
    reg = app.services.registration
    await reg.register(event_id, person(0))
    await reg.register(event_id, person(1))
    result = await reg.register(event_id, person(2), guests=1)
    assert result.status is RegisterStatus.ONLY_ONE_SEAT
    assert await taken(app, event_id) == 2


async def test_плюс_один_при_одном_месте_с_очередью_уходит_в_очередь(app: App) -> None:  # кейс 13
    await enable(app, "plus_one_enabled", "waitlist_enabled")
    event_id = await make_event(app, 3)
    reg = app.services.registration
    await reg.register(event_id, person(0))
    await reg.register(event_id, person(1))
    result = await reg.register(event_id, person(2), guests=1)
    assert (result.status, result.position) == (RegisterStatus.WAITLISTED, 1)
    assert await taken(app, event_id) == 2


async def test_плюс_один_при_полной_встрече_без_очереди_даёт_full(app: App) -> None:
    await enable(app, "plus_one_enabled")
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    result = await app.services.registration.register(event_id, person(1), guests=1)
    assert result.status is RegisterStatus.FULL


async def test_плюс_один_занимает_два_места(app: App) -> None:
    await enable(app, "plus_one_enabled")
    event_id = await make_event(app, 3)
    result = await app.services.registration.register(event_id, person(0), guests=1)
    assert (result.status, result.seats_taken) == (RegisterStatus.OK, 2)


async def test_гость_выключен_по_умолчанию_в_приложении(app: App) -> None:  # кейс 13
    event_id = await make_event(app, 3)
    with pytest.raises(ValueError):
        await app.services.registration.register(event_id, person(0), guests=1)


async def test_гостей_может_быть_только_ноль_или_один(app: App) -> None:
    await enable(app, "plus_one_enabled")
    event_id = await make_event(app, 5)
    with pytest.raises(ValueError):
        await app.services.registration.register(event_id, person(0), guests=2)


async def test_включение_плюс_один_подхватывается_без_перезапуска(app: App) -> None:
    event_id = await make_event(app, 5)
    with pytest.raises(ValueError):
        await app.services.registration.register(event_id, person(0), guests=1)
    await enable(app, "plus_one_enabled")
    result = await app.services.registration.register(event_id, person(0), guests=1)
    assert result.status is RegisterStatus.OK
