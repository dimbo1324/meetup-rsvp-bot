"""ParticipantService: ручное управление участниками встречи (5.7, сценарий 4.7)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from meetup_bot.db.models import Attendance, AuditLog, Ban, BanReason, Registration, User
from meetup_bot.repositories import events
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import InvalidOperation, NotFound, PermissionDenied
from meetup_bot.services.participants import ParticipantService, display_name
from meetup_bot.services.registration import RegisterStatus
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

STRANGER = 777


async def make_event(app: App, capacity: int = 5) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)).event_id


async def taken(app: App, event_id: int) -> int:
    async with app.sessions() as session:
        return await events.seats_taken(session, event_id)


async def reg_of(app: App, event_id: int, user_id: int) -> Registration:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration).where(
                Registration.event_id == event_id, Registration.user_id == user_id
            )
        )
        assert found is not None
        return found


async def allow_dm(app: App, *user_ids: int) -> None:
    async with app.sessions() as session, session.begin():
        for user_id in user_ids:
            user = await session.get(User, user_id)
            assert user is not None
            user.dm_available = True


async def actions(app: App) -> list[str]:
    async with app.sessions() as session:
        return list((await session.scalars(select(AuditLog.action).order_by(AuditLog.id))).all())


def info(n: int) -> UserInfo:
    return person(n)


# ---- admin_add ----


async def test_добавление_пользователя_создаёт_активную_админскую_запись(app: App) -> None:
    event_id = await make_event(app)
    result = await app.services.participants.admin_add(ADMIN_ID, event_id, user=info(1))
    registration = await reg_of(app, event_id, 1001)
    assert (registration.status, registration.source) == ("active", "admin")
    assert (result.seats_taken, result.capacity, result.over_capacity) == (1, 5, False)
    assert result.registration_id == registration.id


async def test_добавление_создаёт_пользователя_в_базе(app: App) -> None:
    event_id = await make_event(app)
    await app.services.participants.admin_add(ADMIN_ID, event_id, user=info(1))
    async with app.sessions() as session:
        user = await session.get(User, 1001)
    assert user is not None
    assert user.first_name == "Участник1"


async def test_добавление_по_имени_создаёт_ручную_запись_без_пользователя(app: App) -> None:
    event_id = await make_event(app)
    await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="  Гость Иван  ")
    async with app.sessions() as session:
        registration = await session.scalar(select(Registration))
    assert registration is not None
    assert (registration.user_id, registration.manual_name) == (None, "Гость Иван")
    assert await taken(app, event_id) == 1


async def test_можно_добавить_нескольких_ручных_с_одинаковым_именем(app: App) -> None:
    event_id = await make_event(app)
    for _ in range(2):
        await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="Иван")
    assert await taken(app, event_id) == 2


async def test_нужен_либо_пользователь_либо_имя(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_add(ADMIN_ID, event_id)
    assert exc.value.code == "who_required"


async def test_одновременно_пользователь_и_имя_нельзя(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_add(
            ADMIN_ID, event_id, user=info(1), manual_name="Иван"
        )
    assert exc.value.code == "who_required"


async def test_пустое_имя_считается_отсутствием(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="   ")
    assert exc.value.code == "who_required"


async def test_добавление_сверх_лимита_без_force_отклоняется(app: App) -> None:
    event_id = await make_event(app, 1)
    await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="A")
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="B")
    assert exc.value.code == "over_capacity"
    assert await taken(app, event_id) == 1


async def test_добавление_сверх_лимита_с_force_разрешено(app: App) -> None:
    event_id = await make_event(app, 1)
    await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="A")
    result = await app.services.participants.admin_add(
        ADMIN_ID, event_id, manual_name="B", force=True
    )
    assert (result.over_capacity, result.seats_taken, result.capacity) == (True, 2, 1)
    assert await taken(app, event_id) == 2


async def test_добавление_уже_записанного_даёт_already_registered(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_add(ADMIN_ID, event_id, user=info(1))
    assert exc.value.code == "already_registered"
    assert await taken(app, event_id) == 1


async def test_добавление_записанного_в_очереди_тоже_даёт_already_registered(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_add(ADMIN_ID, event_id, user=info(1), force=True)
    assert exc.value.code == "already_registered"


async def test_добавленный_админом_не_может_записаться_сам_повторно(app: App) -> None:
    event_id = await make_event(app)
    await app.services.participants.admin_add(ADMIN_ID, event_id, user=info(1))
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.ALREADY


async def test_добавление_отменённого_ранее_участника_допустимо(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.cancel(event_id, 1001)
    await app.services.participants.admin_add(ADMIN_ID, event_id, user=info(1))
    assert await taken(app, event_id) == 1


async def test_добавление_пишет_аудит_и_пересобирает_пост(app: App) -> None:
    event_id = await make_event(app)
    app.refresher.scheduled.clear()
    await app.services.participants.admin_add(ADMIN_ID, event_id, user=info(1))
    assert "registration.admin_add" in await actions(app)
    assert app.refresher.scheduled == [event_id]


async def test_добавление_в_отменённую_встречу_запрещено(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="A")
    assert exc.value.code == "bad_status"


async def test_добавление_в_несуществующую_встречу(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.participants.admin_add(ADMIN_ID, 404, manual_name="A")


async def test_добавление_в_закрытую_админом_встречу_разрешено(app: App) -> None:
    event_id = await make_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="A")
    assert await taken(app, event_id) == 1


async def test_параллельное_добавление_не_превышает_лимит(app: App) -> None:
    event_id = await make_event(app, 3)
    results = await asyncio.gather(
        *(
            app.services.participants.admin_add(ADMIN_ID, event_id, manual_name=f"Гость {n}")
            for n in range(10)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 3
    assert await taken(app, event_id) == 3


# ---- admin_remove ----


async def test_удаление_освобождает_место_без_штрафа(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    result = await app.services.participants.admin_remove(ADMIN_ID, event_id, registration.id)
    removed = await reg_of(app, event_id, 1001)
    assert (removed.status, removed.removed_by) == ("removed", ADMIN_ID)
    assert removed.removed_at == app.clock.now()
    assert removed.attendance == Attendance.UNKNOWN.value
    assert (result.ban_recalc_required, result.banned) == (False, False)
    assert await taken(app, event_id) == 0


async def test_удаление_не_приводит_к_бану(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    await app.services.participants.admin_remove(ADMIN_ID, event_id, registration.id)
    async with app.sessions() as session:
        assert await session.scalar(select(Ban)) is None


async def test_удалённый_может_записаться_снова(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    await app.services.participants.admin_remove(ADMIN_ID, event_id, registration.id)
    assert (await app.services.registration.register(event_id, person(1))).status is (
        RegisterStatus.OK
    )


async def test_удаление_из_очереди_место_не_освобождает_и_никого_не_продвигает(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    for n in range(3):
        await app.services.registration.register(event_id, person(n))
    waiting = await reg_of(app, event_id, 1001)
    result = await app.services.participants.admin_remove(ADMIN_ID, event_id, waiting.id)
    assert result.promoted == ()
    assert (await reg_of(app, event_id, 1002)).status == "waitlist"
    assert await taken(app, event_id) == 1


async def test_удаление_продвигает_очередь_и_пишет_продвинутому(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    await allow_dm(app, 1001)
    active = await reg_of(app, event_id, 1000)
    result = await app.services.participants.admin_remove(ADMIN_ID, event_id, active.id)
    assert [p.user_id for p in result.promoted] == [1001]
    assert (await reg_of(app, event_id, 1001)).status == "active"
    assert len(app.notifier.dms_to(1001)) == 1


async def test_удаление_ручной_записи_возвращает_user_id_none(app: App) -> None:
    event_id = await make_event(app)
    added = await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="Иван")
    result = await app.services.participants.admin_remove(ADMIN_ID, event_id, added.registration_id)
    assert result.user_id is None
    assert await taken(app, event_id) == 0


async def test_удаление_чужой_записи_другой_встречи_не_находит_её(app: App) -> None:
    first = await make_event(app)
    other = (
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=app.clock.now().replace(day=31, hour=20)
        )
    ).event_id
    await app.services.registration.register(first, person(1))
    registration = await reg_of(app, first, 1001)
    with pytest.raises(NotFound):
        await app.services.participants.admin_remove(ADMIN_ID, other, registration.id)


async def test_удаление_несуществующей_записи(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(NotFound):
        await app.services.participants.admin_remove(ADMIN_ID, event_id, 9999)


async def test_повторное_удаление_той_же_записи_отклоняется(app: App) -> None:
    event_id = await make_event(app)
    added = await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="Иван")
    await app.services.participants.admin_remove(ADMIN_ID, event_id, added.registration_id)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_remove(ADMIN_ID, event_id, added.registration_id)
    assert exc.value.code == "not_live"


async def test_удаление_пишет_аудит_и_пересобирает_пост(app: App) -> None:
    event_id = await make_event(app)
    added = await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="Иван")
    app.refresher.scheduled.clear()
    await app.services.participants.admin_remove(ADMIN_ID, event_id, added.registration_id)
    assert (await actions(app))[-1] == "registration.admin_remove"
    assert app.refresher.scheduled == [event_id]


# ---- admin_cancel_after_deadline ----


async def test_отмена_после_дедлайна_без_штрафа_работает_как_удаление(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration.id, as_no_show=False
    )
    after = await reg_of(app, event_id, 1001)
    assert after.status == "removed"
    assert (result.ban_recalc_required, result.banned) == (False, False)
    assert await taken(app, event_id) == 0


async def test_отмена_как_неявка_через_приложение_банит_при_пороге_один(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration.id, as_no_show=True
    )
    after = await reg_of(app, event_id, 1001)
    assert (after.status, after.attendance) == ("cancelled", Attendance.NO_SHOW.value)
    assert after.cancelled_at == app.clock.now()
    assert (result.ban_recalc_required, result.banned) == (True, True)
    async with app.sessions() as session:
        ban = await session.scalar(select(Ban).where(Ban.user_id == 1001))
    assert ban is not None
    assert ban.reason == BanReason.NO_SHOW.value
    assert ban.event_id == event_id


async def test_неявка_освобождает_место(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration.id, as_no_show=True
    )
    assert await taken(app, event_id) == 0


async def test_при_пороге_два_одна_неявка_не_банит(app: App) -> None:
    await app.services.settings.set("ban_after_no_shows", 2, OWNER_ID)
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration.id, as_no_show=True
    )
    assert (result.ban_recalc_required, result.banned) == (True, False)


async def test_при_выключенном_бане_неявка_не_банит(app: App) -> None:
    await app.services.settings.set("ban_after_no_shows", 0, OWNER_ID)
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration.id, as_no_show=True
    )
    assert result.banned is False


async def test_без_ban_service_пересчёт_оставлен_вызывающему(app: App) -> None:
    standalone = ParticipantService(
        app.sessions, app.clock, app.services.locks, app.notifier, app.refresher
    )
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    result = await standalone.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration.id, as_no_show=True
    )
    assert (result.ban_recalc_required, result.banned) == (True, False)
    async with app.sessions() as session:
        assert await session.scalar(select(Ban)) is None


async def test_сбой_пересчёта_бана_не_теряет_отмену(app: App) -> None:
    class BrokenBans:
        async def recalc_for_attendance(self, *args: object) -> None:
            raise RuntimeError("сбой")

    service = ParticipantService(
        app.sessions,
        app.clock,
        app.services.locks,
        app.notifier,
        app.refresher,
        ban_service=BrokenBans(),  # type: ignore[arg-type]
    )
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    registration = await reg_of(app, event_id, 1001)
    result = await service.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration.id, as_no_show=True
    )
    assert (result.ban_recalc_required, result.banned) == (True, False)
    assert (await reg_of(app, event_id, 1001)).status == "cancelled"


async def test_неявка_из_очереди_запрещена(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    waiting = await reg_of(app, event_id, 1001)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.participants.admin_cancel_after_deadline(
            ADMIN_ID, event_id, waiting.id, as_no_show=True
        )
    assert exc.value.code == "no_show_requires_active"


async def test_неявка_ручной_записи_не_требует_пересчёта_бана(app: App) -> None:
    event_id = await make_event(app)
    added = await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="Иван")
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, added.registration_id, as_no_show=True
    )
    assert (result.ban_recalc_required, result.banned) == (False, False)


async def test_неявка_продвигает_очередь(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 1)
    await app.services.registration.register(event_id, person(0))
    await app.services.registration.register(event_id, person(1))
    active = await reg_of(app, event_id, 1000)
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, active.id, as_no_show=True
    )
    assert [p.user_id for p in result.promoted] == [1001]


async def test_неявка_пишет_отдельное_действие_в_аудит(app: App) -> None:
    event_id = await make_event(app)
    added = await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="Иван")
    await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, added.registration_id, as_no_show=True
    )
    assert (await actions(app))[-1] == "registration.cancel_no_show"


# ---- list_participants ----


async def test_список_делит_активных_и_очередь_с_позициями(app: App) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await make_event(app, 2)
    for n in range(5):
        await app.services.registration.register(event_id, person(n))
    listing = await app.services.participants.list_participants(ADMIN_ID, event_id)
    assert [row.user_id for row in listing.active] == [1000, 1001]
    assert [(row.user_id, row.position) for row in listing.waitlist] == [
        (1002, 1),
        (1003, 2),
        (1004, 3),
    ]
    assert (listing.capacity, listing.seats_taken) == (2, 2)


async def test_список_помечает_ручные_записи_и_источник(app: App) -> None:
    event_id = await make_event(app)
    await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="Иван")
    await app.services.participants.admin_add(ADMIN_ID, event_id, user=info(1))
    listing = await app.services.participants.list_participants(ADMIN_ID, event_id)
    manual, regular = listing.active
    assert manual.name == "Иван (вручную)"
    assert (manual.user_id, manual.username, manual.source) == (None, None, "admin")
    assert regular.name == "Участник1"
    assert regular.username == "user1"


async def test_список_не_включает_отменённых_и_удалённых(app: App) -> None:
    event_id = await make_event(app)
    for n in range(3):
        await app.services.registration.register(event_id, person(n))
    await app.services.registration.cancel(event_id, 1000)
    removed = await reg_of(app, event_id, 1001)
    await app.services.participants.admin_remove(ADMIN_ID, event_id, removed.id)
    listing = await app.services.participants.list_participants(ADMIN_ID, event_id)
    assert [row.user_id for row in listing.active] == [1002]


async def test_список_доступен_для_отменённой_встречи(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    await app.services.events.cancel_event(OWNER_ID, event_id)
    listing = await app.services.participants.list_participants(ADMIN_ID, event_id)
    assert len(listing.active) == 1


async def test_список_несуществующей_встречи(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.participants.list_participants(ADMIN_ID, 404)


async def test_список_показывает_гостей_в_строке(app: App) -> None:
    await app.services.settings.set("plus_one_enabled", True, OWNER_ID)
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1), guests=1)
    listing = await app.services.participants.list_participants(ADMIN_ID, event_id)
    assert listing.active[0].guests == 1
    assert listing.seats_taken == 2


# ---- display_name ----


def make_user(first: str, last: str | None, username: str | None) -> User:
    return User(id=1, first_name=first, last_name=last, username=username)


def test_имя_из_имени_и_фамилии() -> None:
    assert display_name(make_user("Анна", "Петрова", "anna"), None) == "Анна Петрова"


def test_имя_только_из_имени() -> None:
    assert display_name(make_user("Анна", None, None), None) == "Анна"


def test_имя_из_username_если_имени_нет() -> None:
    assert display_name(make_user("", None, "anna"), None) == "@anna"


def test_имя_по_умолчанию_если_ничего_нет() -> None:
    assert display_name(make_user("", None, None), None) == "Участник"


def test_ручное_имя_без_названия() -> None:
    assert display_name(None, None) == "Участник (вручную)"


# ---- права ----


async def test_все_методы_участников_запрещены_не_админу(app: App) -> None:
    event_id = await make_event(app)
    added = await app.services.participants.admin_add(ADMIN_ID, event_id, manual_name="Иван")
    service = app.services.participants
    calls = [
        service.admin_add(STRANGER, event_id, manual_name="X"),
        service.admin_remove(STRANGER, event_id, added.registration_id),
        service.admin_cancel_after_deadline(STRANGER, event_id, added.registration_id, False),
        service.admin_cancel_after_deadline(STRANGER, event_id, added.registration_id, True),
        service.list_participants(STRANGER, event_id),
    ]
    for call in calls:
        with pytest.raises(PermissionDenied):
            await call
    assert await taken(app, event_id) == 1


async def test_права_проверяются_раньше_существования_встречи(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.participants.admin_add(STRANGER, 404, manual_name="X")
