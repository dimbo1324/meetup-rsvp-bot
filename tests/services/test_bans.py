"""BanService: ручные баны, разбан, списки, снятие записей и уведомления (раздел 5.8)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from meetup_bot.db.models import AuditLog, Ban, Registration, User
from meetup_bot.services.bans import BAN_DM_TEXT
from meetup_bot.services.errors import InvalidOperation, PermissionDenied
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

FIRST_START = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
STRANGER_ID = 777
TARGET = person(1).id


async def make_event(app: App, week: int = 0, capacity: int = 10) -> int:
    created = await app.services.events.create_event_as_admin(
        OWNER_ID, starts_at=FIRST_START + timedelta(weeks=week), capacity=capacity
    )
    return created.event_id


async def join(app: App, event_id: int, n: int) -> int:
    await app.services.registration.register(event_id, person(n))
    return await registration_of(app, event_id, person(n).id)


async def registration_of(app: App, event_id: int, user_id: int) -> int:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration.id).where(
                Registration.event_id == event_id, Registration.user_id == user_id
            )
        )
    assert found is not None
    return found


async def reg_status(app: App, registration_id: int) -> str:
    async with app.sessions() as session:
        reg = await session.get(Registration, registration_id)
        assert reg is not None
        return reg.status


async def set_rule(app: App, key: str, value: object) -> None:
    await app.services.settings.set(key, value, OWNER_ID)


async def audit_rows(app: App, action: str) -> list[AuditLog]:
    async with app.sessions() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
        )
        return list(rows.all())


async def all_bans(app: App) -> list[Ban]:
    async with app.sessions() as session:
        return list((await session.scalars(select(Ban).order_by(Ban.id))).all())


# --- ban ---


async def test_админ_банит_пользователя(app: App) -> None:
    ban = await app.services.bans.ban(ADMIN_ID, TARGET, comment="спамит")
    assert (ban.user_id, ban.active, ban.reason, ban.comment) == (TARGET, True, "manual", "спамит")
    assert (ban.banned_by, ban.banned_at) == (ADMIN_ID, app.clock.now())


async def test_бан_пишет_аудит(app: App) -> None:
    await app.services.bans.ban(ADMIN_ID, TARGET, comment="спамит")
    [row] = await audit_rows(app, "ban.create")
    assert (row.actor_id, row.target_user_id) == (ADMIN_ID, TARGET)
    assert row.payload == {"reason": "manual", "comment": "спамит", "removed_from_events": []}


async def test_повторный_бан_идемпотентен(app: App) -> None:
    first = await app.services.bans.ban(ADMIN_ID, TARGET)
    second = await app.services.bans.ban(OWNER_ID, TARGET)
    assert second.id == first.id
    assert len(await all_bans(app)) == 1
    assert len(await audit_rows(app, "ban.create")) == 1


async def test_неизвестный_пользователь_получает_заглушку_в_users(app: App) -> None:
    await app.services.bans.ban(ADMIN_ID, 424242)
    async with app.sessions() as session:
        stub = await session.get(User, 424242)
    assert stub is not None
    assert stub.first_name == ""


async def test_заглушка_не_затирает_данные_известного_пользователя(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    async with app.sessions() as session:
        user = await session.get(User, TARGET)
    assert user is not None
    assert user.first_name == "Участник1"


@pytest.mark.parametrize("bad_id", [0, -5])
async def test_некорректный_telegram_id_отклоняется(app: App, bad_id: int) -> None:
    with pytest.raises(InvalidOperation) as error:
        await app.services.bans.ban(ADMIN_ID, bad_id)
    assert error.value.code == "invalid_user"
    assert await all_bans(app) == []


@pytest.mark.parametrize("target", [OWNER_ID, ADMIN_ID])
async def test_нельзя_банить_админа_и_владельца(app: App, target: int) -> None:
    with pytest.raises(InvalidOperation) as error:
        await app.services.bans.ban(OWNER_ID, target)
    assert error.value.code == "cannot_ban_admin"
    assert await all_bans(app) == []


async def test_нельзя_банить_самого_себя(app: App) -> None:
    with pytest.raises(InvalidOperation) as error:
        await app.services.bans.ban(ADMIN_ID, ADMIN_ID)
    assert error.value.code == "cannot_ban_admin"


async def test_ban_не_админ_получает_permission_denied(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.bans.ban(STRANGER_ID, TARGET)
    assert await all_bans(app) == []


async def test_бан_сохраняет_привязку_к_встрече(app: App) -> None:
    event_id = await make_event(app)
    ban = await app.services.bans.ban(ADMIN_ID, TARGET, event_id=event_id)
    assert ban.event_id == event_id


# --- unban ---


async def test_разбан_снимает_действующий_бан(app: App) -> None:
    await app.services.bans.ban(ADMIN_ID, TARGET)
    app.clock.advance(timedelta(hours=1))
    ban = await app.services.bans.unban(OWNER_ID, TARGET)
    assert ban is not None
    assert (ban.active, ban.auto_removed, ban.unbanned_by) == (False, False, OWNER_ID)
    assert ban.unbanned_at == app.clock.now()
    assert [b.active for b in await all_bans(app)] == [False]


async def test_разбан_пишет_аудит(app: App) -> None:
    await app.services.bans.ban(ADMIN_ID, TARGET)
    await app.services.bans.unban(OWNER_ID, TARGET)
    [row] = await audit_rows(app, "ban.remove")
    assert (row.actor_id, row.target_user_id) == (OWNER_ID, TARGET)


async def test_разбан_без_бана_возвращает_none(app: App) -> None:
    assert await app.services.bans.unban(ADMIN_ID, TARGET) is None
    assert await audit_rows(app, "ban.remove") == []


async def test_unban_не_админ_получает_permission_denied(app: App) -> None:
    await app.services.bans.ban(ADMIN_ID, TARGET)
    with pytest.raises(PermissionDenied):
        await app.services.bans.unban(STRANGER_ID, TARGET)
    assert [b.active for b in await all_bans(app)] == [True]


async def test_после_разбана_можно_забанить_снова(app: App) -> None:
    first = await app.services.bans.ban(ADMIN_ID, TARGET)
    await app.services.bans.unban(ADMIN_ID, TARGET)
    second = await app.services.bans.ban(ADMIN_ID, TARGET)
    assert second.id != first.id
    assert [b.active for b in await all_bans(app)] == [False, True]


# --- list_banned и history ---


async def test_список_забаненных_только_действующие_по_порядку(app: App) -> None:
    await app.services.bans.ban(ADMIN_ID, person(1).id)
    app.clock.advance(timedelta(minutes=1))
    await app.services.bans.ban(ADMIN_ID, person(2).id)
    app.clock.advance(timedelta(minutes=1))
    await app.services.bans.ban(ADMIN_ID, person(3).id)
    await app.services.bans.unban(ADMIN_ID, person(2).id)
    banned = await app.services.bans.list_banned(ADMIN_ID)
    assert [b.user_id for b in banned] == [person(1).id, person(3).id]


async def test_список_забаненных_пуст_без_банов(app: App) -> None:
    assert await app.services.bans.list_banned(OWNER_ID) == []


async def test_list_banned_не_админ_получает_permission_denied(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.bans.list_banned(STRANGER_ID)


async def test_история_включает_снятые_баны_только_этого_пользователя(app: App) -> None:
    await app.services.bans.ban(ADMIN_ID, TARGET)
    app.clock.advance(timedelta(minutes=1))
    await app.services.bans.unban(ADMIN_ID, TARGET)
    app.clock.advance(timedelta(minutes=1))
    await app.services.bans.ban(ADMIN_ID, TARGET)
    await app.services.bans.ban(ADMIN_ID, person(2).id)
    history = await app.services.bans.history(ADMIN_ID, TARGET)
    assert [b.active for b in history] == [False, True]
    assert {b.user_id for b in history} == {TARGET}


async def test_history_не_админ_получает_permission_denied(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.bans.history(STRANGER_ID, TARGET)


# --- снятие записей забаненного ---


async def test_бан_снимает_записи_с_будущих_встреч(app: App) -> None:
    first, second = await make_event(app, 0), await make_event(app, 1)
    regs = [await join(app, first, 1), await join(app, second, 1)]
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert [await reg_status(app, r) for r in regs] == ["removed", "removed"]
    async with app.sessions() as session:
        reg = await session.get(Registration, regs[0])
    assert reg is not None
    assert (reg.removed_by, reg.removed_at) == (ADMIN_ID, app.clock.now())


async def test_бан_пересобирает_посты_затронутых_встреч(app: App) -> None:
    first, second = await make_event(app, 0), await make_event(app, 1)
    await join(app, first, 1)
    await join(app, second, 1)
    app.refresher.scheduled.clear()
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert app.refresher.scheduled == [first, second]


async def test_аудит_бана_перечисляет_затронутые_встречи(app: App) -> None:
    first, second = await make_event(app, 0), await make_event(app, 1)
    await join(app, first, 1)
    await join(app, second, 1)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    [row] = await audit_rows(app, "ban.create")
    assert row.payload["removed_from_events"] == [first, second]


async def test_при_выключенной_настройке_записи_забаненного_остаются(app: App) -> None:
    await set_rule(app, "remove_banned_from_future_events", False)
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    app.refresher.scheduled.clear()
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert await reg_status(app, registration_id) == "active"
    assert app.refresher.scheduled == []


async def test_бан_не_трогает_начавшуюся_встречу(app: App) -> None:
    first, second = await make_event(app, 0), await make_event(app, 1)
    started = await join(app, first, 1)
    future = await join(app, second, 1)
    app.clock.advance(FIRST_START + timedelta(hours=1) - app.clock.now())
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert await reg_status(app, started) == "active"
    assert await reg_status(app, future) == "removed"


async def test_бан_не_трогает_записи_других_участников(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    other = await join(app, event_id, 2)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert await reg_status(app, other) == "active"


async def test_бан_снимает_человека_из_листа_ожидания(app: App) -> None:
    await set_rule(app, "waitlist_enabled", True)
    event_id = await make_event(app, capacity=1)
    await join(app, event_id, 2)
    waiting = await join(app, event_id, 1)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert await reg_status(app, waiting) == "removed"


async def test_освободившееся_место_достаётся_следующему_из_очереди(app: App) -> None:
    await set_rule(app, "waitlist_enabled", True)
    event_id = await make_event(app, capacity=1)
    await join(app, event_id, 1)
    waiting = await join(app, event_id, 2)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert await reg_status(app, waiting) == "active"
    assert len(app.notifier.dms_to(person(2).id)) == 1


async def test_уход_из_очереди_забаненного_не_продвигает_других(app: App) -> None:
    await set_rule(app, "waitlist_enabled", True)
    event_id = await make_event(app, capacity=1)
    await join(app, event_id, 2)
    await join(app, event_id, 1)
    third = await join(app, event_id, 3)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    # Освободилось не место, а позиция в очереди: активный остался на месте
    assert await reg_status(app, third) == "waitlist"
    assert app.notifier.dms_to(person(3).id) == []


# --- уведомление забаненного ---


async def test_по_умолчанию_забаненному_сообщение_не_отправляется(app: App) -> None:
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert app.notifier.dms_to(TARGET) == []


async def test_notify_user_on_ban_отправляет_сообщение(app: App) -> None:
    await set_rule(app, "notify_user_on_ban", True)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert app.notifier.dms_to(TARGET) == [BAN_DM_TEXT]


async def test_повторный_бан_не_шлёт_второе_сообщение(app: App) -> None:
    await set_rule(app, "notify_user_on_ban", True)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    await app.services.bans.ban(ADMIN_ID, TARGET)
    assert app.notifier.dms_to(TARGET) == [BAN_DM_TEXT]


async def test_недоставленное_сообщение_не_отменяет_бан(app: App) -> None:
    await set_rule(app, "notify_user_on_ban", True)
    app.notifier.blocked.add(TARGET)
    ban = await app.services.bans.ban(ADMIN_ID, TARGET)
    assert ban.active
    assert app.notifier.dms_to(TARGET) == []


# --- автобан напрямую ---


async def test_recalc_for_attendance_без_неявок_ничего_не_делает(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    outcome = await app.services.bans.recalc_for_attendance(ADMIN_ID, TARGET, event_id)
    assert (outcome.ban, outcome.banned, outcome.unbanned) == (None, False, False)
