"""Отметка явки и автобан по неявкам (разделы 5.7, 5.8; кейсы 7-10 и 14 из 16.2)."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from meetup_bot.db.models import (
    Attendance,
    AuditLog,
    Ban,
    Event,
    Registration,
    RegistrationStatus,
)
from meetup_bot.repositories import bans as bans_repo
from meetup_bot.services.attendance import ATTENDANCE_WINDOW, AlreadyMarked
from meetup_bot.services.bans import BanOutcome
from meetup_bot.services.errors import InvalidOperation, NotFound, PermissionDenied
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

# Явно заданное начало (суббота 24.10 17:00 по Монтевидео) позже фикстуры clock (четверг
# 2026-10-22); дальше шаг в неделю
FIRST_START = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
STRANGER_ID = 777


async def make_event(app: App, week: int = 0, capacity: int = 10) -> int:
    created = await app.services.events.create_event_as_admin(
        OWNER_ID, starts_at=FIRST_START + timedelta(weeks=week), capacity=capacity
    )
    return created.event_id


async def start_of(app: App, event_id: int) -> datetime:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        return event.starts_at


async def advance_to_after_start(app: App, event_id: int) -> None:
    """Часы — на закрытие записи (начало + час), встреча переведена в finished.

    Явка отмечается только у завершённой встречи; статус ставит планировщик, поэтому тест
    вызывает его шаг вручную (только вперёд по времени).
    """
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        target = max(event.starts_at, event.registration_closes_at)
    app.clock.advance(target - app.clock.now())
    await app.services.lifecycle.finish_due(app.clock.now())


async def join(app: App, event_id: int, n: int) -> int:
    """Записывает участника person(n); возвращает id записи."""
    await app.services.registration.register(event_id, person(n))
    return await reg_id(app, event_id, person(n).id)


async def reg_id(app: App, event_id: int, user_id: int) -> int:
    async with app.sessions() as session:
        found = await session.scalar(
            select(Registration.id)
            .where(Registration.event_id == event_id, Registration.user_id == user_id)
            .order_by(Registration.id.desc())
        )
    assert found is not None
    return found


async def set_rule(app: App, key: str, value: object) -> None:
    await app.services.settings.set(key, value, OWNER_ID)


async def bans_of(app: App, user_id: int) -> list[Ban]:
    async with app.sessions() as session:
        return await bans_repo.history(session, user_id)


async def active_ban(app: App, user_id: int) -> Ban | None:
    async with app.sessions() as session:
        return await bans_repo.get_active(session, user_id)


async def attendance_of(app: App, registration_id: int) -> str:
    async with app.sessions() as session:
        reg = await session.get(Registration, registration_id)
        assert reg is not None
        return reg.attendance


async def audit_actions(app: App) -> list[str]:
    async with app.sessions() as session:
        return list((await session.scalars(select(AuditLog.action).order_by(AuditLog.id))).all())


async def no_show_on(app: App, event_id: int, n: int) -> None:
    """Участник person(n) записан и отмечен неявкой на встрече (часы сдвигаются к её началу)."""
    registration_id = await reg_id(app, event_id, person(n).id)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})


# --- кейс 7: порог автобана ---


async def test_порог_1_бан_после_первой_неявки(app: App) -> None:  # кейс 7
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    result = await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})
    assert result.new_bans == (person(1).id,)
    ban = await active_ban(app, person(1).id)
    assert ban is not None
    assert (ban.reason, ban.event_id, ban.banned_by) == ("no_show", event_id, ADMIN_ID)


async def test_порог_2_после_первой_неявки_бана_нет(app: App) -> None:  # кейс 7
    await set_rule(app, "ban_after_no_shows", 2)
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await no_show_on(app, event_id, 1)
    assert await active_ban(app, person(1).id) is None


async def test_порог_2_бан_после_второй_неявки(app: App) -> None:  # кейс 7
    await set_rule(app, "ban_after_no_shows", 2)
    first, second = await make_event(app, 0), await make_event(app, 1)
    await join(app, first, 1)
    await join(app, second, 1)
    await no_show_on(app, first, 1)
    await no_show_on(app, second, 1)
    ban = await active_ban(app, person(1).id)
    assert ban is not None
    assert (ban.reason, ban.event_id) == ("no_show", second)


async def test_порог_0_отключает_автобан(app: App) -> None:
    await set_rule(app, "ban_after_no_shows", 0)
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await no_show_on(app, event_id, 1)
    assert await bans_of(app, person(1).id) == []


async def test_неявка_одного_не_банит_пришедшего(app: App) -> None:
    event_id = await make_event(app)
    absent = await join(app, event_id, 1)
    await join(app, event_id, 2)
    await advance_to_after_start(app, event_id)
    result = await app.services.attendance.mark(ADMIN_ID, event_id, {absent})
    assert result.no_shows == (absent,)
    assert await active_ban(app, person(2).id) is None
    assert await active_ban(app, person(1).id) is not None


# --- кейс 8: разбан и счётчик ---


async def test_после_ручного_разбана_счётчик_неявок_обнуляется(app: App) -> None:  # кейс 8
    await set_rule(app, "ban_after_no_shows", 2)
    # Без снятия будущих записей, иначе бан после второй неявки убрал бы человека с третьей
    await set_rule(app, "remove_banned_from_future_events", False)
    first, second, third = [await make_event(app, week) for week in range(3)]
    for event_id in (first, second, third):
        await join(app, event_id, 1)
    await no_show_on(app, first, 1)
    await no_show_on(app, second, 1)
    assert await active_ban(app, person(1).id) is not None

    await app.services.bans.unban(OWNER_ID, person(1).id)
    await no_show_on(app, third, 1)
    assert await active_ban(app, person(1).id) is None


async def test_автоснятие_бана_при_исправлении_явки_счётчик_не_обнуляет(app: App) -> None:  # кейс 8
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})

    fixed = await app.services.attendance.correct(
        ADMIN_ID, event_id, registration_id, Attendance.ATTENDED
    )
    assert fixed.unbanned
    assert await active_ban(app, person(1).id) is None

    again = await app.services.attendance.correct(
        ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW
    )
    assert again.banned
    assert await active_ban(app, person(1).id) is not None
    assert len(await bans_of(app, person(1).id)) == 2


async def test_автоснятие_бана_не_стирает_прежние_неявки(app: App) -> None:  # кейс 8
    await set_rule(app, "ban_after_no_shows", 2)
    await set_rule(app, "remove_banned_from_future_events", False)
    first, second, third = [await make_event(app, week) for week in range(3)]
    for event_id in (first, second, third):
        await join(app, event_id, 1)
    await no_show_on(app, first, 1)
    second_reg = await reg_id(app, second, person(1).id)
    await no_show_on(app, second, 1)
    assert await active_ban(app, person(1).id) is not None

    await app.services.attendance.correct(ADMIN_ID, second, second_reg, Attendance.ATTENDED)
    assert await active_ban(app, person(1).id) is None
    # Первая неявка осталась в счёте, поэтому третья снова доводит до порога
    await no_show_on(app, third, 1)
    assert await active_ban(app, person(1).id) is not None


# --- кейс 9: исправление явки ---


async def test_исправление_неявки_на_пришёл_снимает_автобан(app: App) -> None:  # кейс 9
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})

    outcome = await app.services.attendance.correct(
        ADMIN_ID, event_id, registration_id, Attendance.ATTENDED
    )
    assert (outcome.unbanned, outcome.banned) == (True, False)
    [ban] = await bans_of(app, person(1).id)
    assert (ban.active, ban.auto_removed, ban.unbanned_by) == (False, True, ADMIN_ID)
    assert await attendance_of(app, registration_id) == "attended"


async def test_исправление_явки_не_снимает_ручной_бан(app: App) -> None:  # кейс 9
    await set_rule(app, "remove_banned_from_future_events", False)
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await app.services.bans.ban(OWNER_ID, person(1).id)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})

    outcome = await app.services.attendance.correct(
        ADMIN_ID, event_id, registration_id, Attendance.ATTENDED
    )
    assert (outcome.banned, outcome.unbanned) == (False, False)
    ban = await active_ban(app, person(1).id)
    assert ban is not None
    assert ban.reason == "manual"


async def test_автобан_за_другую_встречу_снимается_ниже_порога(app: App) -> None:  # кейс 9
    await set_rule(app, "ban_after_no_shows", 2)
    await set_rule(app, "remove_banned_from_future_events", False)
    # Встречи подряд в соседние дни, чтобы окно исправления первой ещё не закрылось
    first = await make_event(app, 0)
    created = await app.services.events.create_event_as_admin(
        OWNER_ID, starts_at=FIRST_START + timedelta(days=1)
    )
    second = created.event_id
    first_reg = await join(app, first, 1)
    await join(app, second, 1)
    await no_show_on(app, first, 1)
    await no_show_on(app, second, 1)
    ban = await active_ban(app, person(1).id)
    assert ban is not None
    assert ban.event_id == second

    # Бан выдан за вторую встречу, но после исправления первой неявок меньше порога (4.6)
    outcome = await app.services.attendance.correct(ADMIN_ID, first, first_reg, Attendance.ATTENDED)
    assert outcome.unbanned
    assert await active_ban(app, person(1).id) is None


async def test_исправление_на_неявку_выдаёт_бан(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert await active_ban(app, person(1).id) is None

    outcome = await app.services.attendance.correct(
        ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW
    )
    assert outcome.banned
    assert await active_ban(app, person(1).id) is not None


# --- кейс 10: поздние продвижения ---


async def waitlisted_late_promoted(app: App, late_hours: int) -> tuple[int, int]:
    """Встреча на 1 место: person(1) отпущен админом, person(2) продвинут из очереди."""
    await set_rule(app, "waitlist_enabled", True)
    await set_rule(app, "late_promotion_hours", late_hours)
    event_id = await make_event(app, capacity=1)
    first = await join(app, event_id, 1)
    await join(app, event_id, 2)
    await app.services.participants.admin_remove(OWNER_ID, event_id, first)
    return event_id, await reg_id(app, event_id, person(2).id)


async def test_неявка_по_записи_late_promoted_не_засчитывается(app: App) -> None:  # кейс 10
    event_id, promoted_reg = await waitlisted_late_promoted(app, late_hours=72)
    async with app.sessions() as session:
        reg = await session.get(Registration, promoted_reg)
        assert reg is not None
        assert (reg.status, reg.late_promoted) == ("active", True)

    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {promoted_reg})
    assert await attendance_of(app, promoted_reg) == "no_show"
    assert await count_no_shows_of(app, person(2).id) == 0
    assert await active_ban(app, person(2).id) is None


async def count_no_shows_of(app: App, user_id: int) -> int:
    async with app.sessions() as session:
        return await bans_repo.count_no_shows(session, user_id)


async def test_неявка_по_обычному_продвижению_засчитывается(app: App) -> None:  # кейс 10
    event_id, promoted_reg = await waitlisted_late_promoted(app, late_hours=0)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {promoted_reg})
    assert await count_no_shows_of(app, person(2).id) == 1
    assert await active_ban(app, person(2).id) is not None


# --- кейс 14: отменённая встреча ---


async def test_отметка_явки_для_отменённой_встречи_запрещена(app: App) -> None:  # кейс 14
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    await advance_to_after_start(app, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert error.value.code == "event_cancelled"


async def test_ведомость_отменённой_встречи_недоступна(app: App) -> None:  # кейс 14
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    await advance_to_after_start(app, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.get_sheet(ADMIN_ID, event_id)
    assert error.value.code == "event_cancelled"


async def test_исправление_явки_отменённой_встречи_запрещено(app: App) -> None:  # кейс 14
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})
    await app.services.events.cancel_event(OWNER_ID, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.correct(
            ADMIN_ID, event_id, registration_id, Attendance.ATTENDED
        )
    assert error.value.code == "event_cancelled"


async def test_неявка_по_отменённой_встрече_не_считается(app: App) -> None:  # кейс 14
    await set_rule(app, "ban_after_no_shows", 2)
    await set_rule(app, "remove_banned_from_future_events", False)
    first, second = await make_event(app, 0), await make_event(app, 1)
    await join(app, first, 1)
    await join(app, second, 1)
    await no_show_on(app, first, 1)
    await app.services.events.cancel_event(OWNER_ID, first)
    assert await count_no_shows_of(app, person(1).id) == 0

    await no_show_on(app, second, 1)
    assert await count_no_shows_of(app, person(1).id) == 1
    assert await active_ban(app, person(1).id) is None


async def test_expire_unmarked_пропускает_отменённую_встречу(app: App) -> None:  # кейс 14
    event_id = await make_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    later = await start_of(app, event_id) + ATTENDANCE_WINDOW + timedelta(minutes=1)
    assert await app.services.attendance.expire_unmarked(later) == []


# --- неявка при отмене админом после дедлайна ---


async def test_отмена_как_неявка_банит_по_порогу(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration_id, as_no_show=True
    )
    assert result.banned
    ban = await active_ban(app, person(1).id)
    assert ban is not None
    assert (ban.reason, ban.event_id) == ("no_show", event_id)


async def test_отмена_как_неявка_при_пороге_2_не_банит(app: App) -> None:
    await set_rule(app, "ban_after_no_shows", 2)
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration_id, as_no_show=True
    )
    assert not result.banned
    assert await active_ban(app, person(1).id) is None


async def test_отмена_без_штрафа_не_банит(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    result = await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration_id, as_no_show=False
    )
    assert not result.banned
    assert await bans_of(app, person(1).id) == []


async def test_отметка_явки_после_отмены_как_неявка_не_дублирует_бан(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration_id, as_no_show=True
    )
    await advance_to_after_start(app, event_id)
    result = await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert result.new_bans == ()
    assert len(await bans_of(app, person(1).id)) == 1


async def test_исправление_отменённой_как_неявка_записи_снимает_бан(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration_id, as_no_show=True
    )
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())

    outcome = await app.services.attendance.correct(
        ADMIN_ID, event_id, registration_id, Attendance.ATTENDED
    )
    assert outcome.unbanned
    assert await active_ban(app, person(1).id) is None


# --- get_sheet ---


async def test_ведомость_недоступна_пока_запись_не_закрыта(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.get_sheet(ADMIN_ID, event_id)
    assert error.value.code == "attendance_not_yet"


async def test_ведомость_содержит_активных_участников_в_порядке_записи(app: App) -> None:
    event_id = await make_event(app)
    first = await join(app, event_id, 1)
    second = await join(app, event_id, 2)
    cancelled = await join(app, event_id, 3)
    await app.services.registration.cancel(event_id, person(3).id)
    await advance_to_after_start(app, event_id)

    sheet = await app.services.attendance.get_sheet(ADMIN_ID, event_id)
    assert [row.registration_id for row in sheet] == [first, second]
    assert cancelled not in {row.registration_id for row in sheet}
    assert (sheet[0].name, sheet[0].user_id, sheet[0].attendance) == (
        "Участник1",
        person(1).id,
        "unknown",
    )


async def test_в_ведомости_вручную_добавленный_помечен(app: App) -> None:
    event_id = await make_event(app)
    await app.services.participants.admin_add(OWNER_ID, event_id, manual_name="Гость Гостев")
    await advance_to_after_start(app, event_id)
    [row] = await app.services.attendance.get_sheet(ADMIN_ID, event_id)
    assert row.name == "Гость Гостев (вручную)"
    assert row.user_id is None


async def test_ведомость_несуществующей_встречи(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.attendance.get_sheet(ADMIN_ID, 9999)


async def test_ведомость_только_для_админа(app: App) -> None:
    event_id = await make_event(app)
    await advance_to_after_start(app, event_id)
    with pytest.raises(PermissionDenied):
        await app.services.attendance.get_sheet(STRANGER_ID, event_id)


# --- mark ---


async def test_пустой_набор_неявок_означает_что_пришли_все(app: App) -> None:
    event_id = await make_event(app)
    regs = [await join(app, event_id, n) for n in (1, 2, 3)]
    await advance_to_after_start(app, event_id)
    result = await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert (result.no_shows, result.new_bans, result.failed_ban_user_ids) == ((), (), ())
    assert [await attendance_of(app, r) for r in regs] == ["attended"] * 3
    assert await bans_of(app, person(1).id) == []


async def test_mark_записывает_кто_и_когда_отметил(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
    assert event is not None
    assert (event.attendance_marked_by, event.attendance_marked_at) == (
        ADMIN_ID,
        app.clock.now(),
    )


async def test_первый_нажавший_готово_побеждает(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())

    with pytest.raises(AlreadyMarked) as error:
        await app.services.attendance.mark(OWNER_ID, event_id, {registration_id})
    assert error.value.marked_by_id == ADMIN_ID
    assert error.value.marked_by_name == "Админ"
    assert await attendance_of(app, registration_id) == "attended"


async def test_already_marked_после_автозакрытия_без_имени(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    later = await start_of(app, event_id) + ATTENDANCE_WINDOW + timedelta(minutes=1)
    await app.services.attendance.expire_unmarked(later)
    app.clock.advance(later - app.clock.now())
    with pytest.raises(AlreadyMarked) as error:
        await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert (error.value.marked_by_id, error.value.marked_by_name) == (None, None)


async def test_mark_до_закрытия_записи_запрещён(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert error.value.code == "attendance_not_yet"


async def test_mark_через_7_суток_и_минуту_закрыт_окном(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    app.clock.advance(await start_of(app, event_id) + ATTENDANCE_WINDOW - app.clock.now())
    app.clock.advance(timedelta(minutes=1))
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert error.value.code == "attendance_window_closed"


async def test_mark_ровно_на_границе_окна_ещё_разрешён(app: App) -> None:
    event_id = await make_event(app)
    await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    app.clock.advance(await start_of(app, event_id) + ATTENDANCE_WINDOW - app.clock.now())
    result = await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert result.failed_ban_user_ids == ()


async def test_чужие_registration_id_отклоняются_и_ничего_не_меняют(app: App) -> None:
    first, second = await make_event(app, 0), await make_event(app, 1)
    mine = await join(app, first, 1)
    foreign = await join(app, second, 2)
    await advance_to_after_start(app, first)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.mark(ADMIN_ID, first, {mine, foreign})
    assert error.value.code == "unknown_registration"
    assert await attendance_of(app, mine) == "unknown"
    async with app.sessions() as session:
        event = await session.get(Event, first)
    assert event is not None
    assert event.attendance_marked_at is None


async def test_mark_отклоняет_несуществующий_registration_id(app: App) -> None:
    event_id = await make_event(app)
    await advance_to_after_start(app, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.mark(ADMIN_ID, event_id, {424242})
    assert error.value.code == "unknown_registration"


async def test_mark_не_учитывает_лист_ожидания_и_отменённых(app: App) -> None:
    await set_rule(app, "waitlist_enabled", True)
    event_id = await make_event(app, capacity=1)
    active = await join(app, event_id, 1)
    waiting = await join(app, event_id, 2)
    await advance_to_after_start(app, event_id)
    with pytest.raises(InvalidOperation):
        await app.services.attendance.mark(ADMIN_ID, event_id, {waiting})
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    assert await attendance_of(app, active) == "attended"
    assert await attendance_of(app, waiting) == "unknown"


async def test_mark_только_для_админа(app: App) -> None:
    event_id = await make_event(app)
    await advance_to_after_start(app, event_id)
    with pytest.raises(PermissionDenied):
        await app.services.attendance.mark(STRANGER_ID, event_id, set())


async def test_mark_несуществующей_встречи(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.attendance.mark(ADMIN_ID, 9999, set())


async def test_mark_пишет_аудит(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})
    async with app.sessions() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "attendance.mark"))
    assert row is not None
    assert (row.actor_id, row.event_id) == (ADMIN_ID, event_id)
    assert row.payload == {"no_show_registration_ids": [registration_id]}


async def test_вручную_добавленный_неявившийся_не_банится(app: App) -> None:
    event_id = await make_event(app)
    await app.services.participants.admin_add(OWNER_ID, event_id, manual_name="Без аккаунта")
    await advance_to_after_start(app, event_id)
    [row] = await app.services.attendance.get_sheet(ADMIN_ID, event_id)
    result = await app.services.attendance.mark(ADMIN_ID, event_id, {row.registration_id})
    assert (result.no_shows, result.new_bans) == ((row.registration_id,), ())
    async with app.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Ban)) == 0


# --- correct ---


async def test_исправление_до_отметки_явки_даёт_not_marked(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.correct(
            ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW
        )
    assert error.value.code == "not_marked"


async def test_исправление_на_unknown_запрещено(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.correct(
            ADMIN_ID, event_id, registration_id, Attendance.UNKNOWN
        )
    assert error.value.code == "invalid_attendance"


async def test_исправление_после_окна_запрещено(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    app.clock.advance(ATTENDANCE_WINDOW)
    with pytest.raises(InvalidOperation) as error:
        await app.services.attendance.correct(
            ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW
        )
    assert error.value.code == "attendance_window_closed"
    assert await attendance_of(app, registration_id) == "attended"


async def test_исправление_чужой_записи_даёт_not_found(app: App) -> None:
    first, second = await make_event(app, 0), await make_event(app, 1)
    await join(app, first, 1)
    foreign = await join(app, second, 2)
    await advance_to_after_start(app, first)
    await app.services.attendance.mark(ADMIN_ID, first, set())
    with pytest.raises(NotFound):
        await app.services.attendance.correct(ADMIN_ID, first, foreign, Attendance.NO_SHOW)


async def test_исправление_несуществующей_записи_даёт_not_found(app: App) -> None:
    event_id = await make_event(app)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    with pytest.raises(NotFound):
        await app.services.attendance.correct(ADMIN_ID, event_id, 424242, Attendance.NO_SHOW)


async def test_исправление_снятой_записи_даёт_not_found(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await app.services.participants.admin_remove(OWNER_ID, event_id, registration_id)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    with pytest.raises(NotFound):
        await app.services.attendance.correct(
            ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW
        )


async def test_исправление_записи_без_неявки_отменённой_участником_даёт_not_found(
    app: App,
) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await app.services.registration.cancel(event_id, person(1).id)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    with pytest.raises(NotFound):
        await app.services.attendance.correct(
            ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW
        )


async def test_исправление_только_для_админа(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    with pytest.raises(PermissionDenied):
        await app.services.attendance.correct(
            STRANGER_ID, event_id, registration_id, Attendance.NO_SHOW
        )


async def test_исправление_записи_без_пользователя_не_трогает_баны(app: App) -> None:
    event_id = await make_event(app)
    await app.services.participants.admin_add(OWNER_ID, event_id, manual_name="Без аккаунта")
    await advance_to_after_start(app, event_id)
    [row] = await app.services.attendance.get_sheet(ADMIN_ID, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    outcome = await app.services.attendance.correct(
        ADMIN_ID, event_id, row.registration_id, Attendance.NO_SHOW
    )
    assert outcome == BanOutcome(None)


async def test_исправление_пишет_аудит(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, set())
    await app.services.attendance.correct(ADMIN_ID, event_id, registration_id, Attendance.NO_SHOW)
    async with app.sessions() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "attendance.correct"))
    assert row is not None
    assert row.target_user_id == person(1).id
    assert row.payload == {
        "registration_id": registration_id,
        "from": "attended",
        "to": "no_show",
    }


# --- expire_unmarked ---


async def test_expire_unmarked_через_7_суток_отмечает_всех_пришедшими(app: App) -> None:
    event_id = await make_event(app)
    regs = [await join(app, event_id, n) for n in (1, 2)]
    await advance_to_after_start(app, event_id)
    later = await start_of(app, event_id) + ATTENDANCE_WINDOW + timedelta(minutes=1)
    app.clock.advance(later - app.clock.now())

    assert await app.services.attendance.expire_unmarked(app.clock.now()) == [event_id]
    assert [await attendance_of(app, r) for r in regs] == ["attended", "attended"]
    for n in (1, 2):
        assert await bans_of(app, person(n).id) == []
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
    assert event is not None
    assert (event.attendance_marked_at, event.attendance_marked_by) == (later, None)
    assert "attendance.expire" in await audit_actions(app)


async def test_expire_unmarked_раньше_7_суток_ничего_не_делает(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    almost = await start_of(app, event_id) + ATTENDANCE_WINDOW
    assert await app.services.attendance.expire_unmarked(almost) == []
    assert await attendance_of(app, registration_id) == "unknown"


async def test_expire_unmarked_не_трогает_уже_отмеченную_встречу(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})
    later = await start_of(app, event_id) + ATTENDANCE_WINDOW + timedelta(days=1)
    assert await app.services.attendance.expire_unmarked(later) == []
    assert await attendance_of(app, registration_id) == "no_show"


async def test_expire_unmarked_повторный_вызов_пуст(app: App) -> None:
    event_id = await make_event(app)
    await advance_to_after_start(app, event_id)
    later = await start_of(app, event_id) + ATTENDANCE_WINDOW + timedelta(minutes=1)
    assert await app.services.attendance.expire_unmarked(later) == [event_id]
    assert await app.services.attendance.expire_unmarked(later) == []


# --- retry_pending_bans и сбои пересчёта ---


def failing_recalc(app: App, fail_for: set[int] | None = None) -> None:
    """Подменяет пересчёт банов: бросает для `fail_for` (или для всех, если None)."""
    original = app.services.bans.recalc_for_attendance

    async def recalc(actor_id: int | None, user_id: int, event_id: int) -> BanOutcome:
        if fail_for is None or user_id in fail_for:
            raise RuntimeError("сбой пересчёта")
        return await original(actor_id, user_id, event_id)

    app.services.bans.recalc_for_attendance = recalc  # type: ignore[method-assign]


async def test_сбой_пересчёта_попадает_в_failed_ban_user_ids(
    app: App, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)

    async def boom(*args: object, **kwargs: object) -> BanOutcome:
        raise RuntimeError("сбой пересчёта")

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", boom)
    with caplog.at_level(logging.ERROR):
        result = await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})
    assert result.failed_ban_user_ids == (person(1).id,)
    assert result.new_bans == ()
    # Сама отметка уже сохранена
    assert await attendance_of(app, registration_id) == "no_show"
    assert await active_ban(app, person(1).id) is None


async def test_сбой_одного_пересчёта_не_мешает_остальным(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await make_event(app)
    regs = {await join(app, event_id, 1), await join(app, event_id, 2)}
    await advance_to_after_start(app, event_id)
    original = app.services.bans.recalc_for_attendance

    async def flaky(actor_id: int | None, user_id: int, event_id: int) -> BanOutcome:
        if user_id == person(1).id:
            raise RuntimeError("сбой пересчёта")
        return await original(actor_id, user_id, event_id)

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", flaky)
    result = await app.services.attendance.mark(ADMIN_ID, event_id, regs)
    assert result.failed_ban_user_ids == (person(1).id,)
    assert result.new_bans == (person(2).id,)


async def test_retry_pending_bans_дожимает_пропущенные_баны_идемпотентно(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)

    async def boom(*args: object, **kwargs: object) -> BanOutcome:
        raise RuntimeError("сбой пересчёта")

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", boom)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})
    monkeypatch.undo()

    assert await app.services.attendance.retry_pending_bans() == [event_id]
    assert await app.services.attendance.retry_pending_bans() == []
    assert len(await bans_of(app, person(1).id)) == 1


async def test_retry_pending_bans_терпит_сбой_и_продолжает(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await make_event(app)
    regs = {await join(app, event_id, 1), await join(app, event_id, 2)}
    await advance_to_after_start(app, event_id)

    async def boom(*args: object, **kwargs: object) -> BanOutcome:
        raise RuntimeError("сбой пересчёта")

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", boom)
    await app.services.attendance.mark(ADMIN_ID, event_id, regs)
    monkeypatch.undo()
    failing_recalc(app, {person(1).id})
    # Встреча не обработана целиком: её дожмут снова, но второй пользователь уже забанен
    assert await app.services.attendance.retry_pending_bans() == []
    assert await active_ban(app, person(2).id) is not None
    assert await active_ban(app, person(1).id) is None


async def test_retry_pending_bans_для_неотмеченной_встречи_пуст(app: App) -> None:
    event_id = await make_event(app)
    await advance_to_after_start(app, event_id)
    assert await app.services.attendance.retry_pending_bans() == []


async def test_порог_0_после_отметки_ничего_не_дожимает(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await set_rule(app, "ban_after_no_shows", 0)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})
    assert await app.services.attendance.retry_pending_bans() == []
    assert await bans_of(app, person(1).id) == []


async def test_статус_записи_неявившегося_остаётся_active(app: App) -> None:
    event_id = await make_event(app)
    registration_id = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})
    async with app.sessions() as session:
        reg = await session.get(Registration, registration_id)
    assert reg is not None
    assert reg.status == RegistrationStatus.ACTIVE.value


async def test_запись_забаненного_на_будущую_встречу_снимается_после_автобана(app: App) -> None:
    first, second = await make_event(app, 0), await make_event(app, 1)
    await join(app, first, 1)
    future = await join(app, second, 1)
    await no_show_on(app, first, 1)
    async with app.sessions() as session:
        reg = await session.get(Registration, future)
    assert reg is not None
    assert reg.status == RegistrationStatus.REMOVED.value
