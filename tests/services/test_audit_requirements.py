"""Требования, найденные аудитом: как бот ДОЛЖЕН себя вести (код подгоняется под эти тесты)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from meetup_bot.db.models import Attendance, Event, User
from meetup_bot.repositories import users as users_repo
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import InvalidOperation
from meetup_bot.services.registration import CancelStatus, RegisterStatus
from tests.helpers import OWNER_ID, App, person
from tests.services.test_attendance import (
    FIRST_START,
    active_ban,
    advance_to_after_start,
    join,
    make_event,
    set_rule,
)


async def enable_dm(app: App, *user_ids: int) -> None:
    async with app.sessions() as session, session.begin():
        for user_id in user_ids:
            await users_repo.set_dm_available(session, user_id, True, app.clock.now())


# --- Баны и явка -------------------------------------------------------------------------


async def test_автобан_снимается_когда_неявок_стало_меньше_порога_по_любой_встрече(
    app: App,
) -> None:
    """Порог 2: неявки на A и B, бан выдан по B. Исправили A — оснований для бана больше нет."""
    await set_rule(app, "ban_after_no_shows", 2)
    first = await make_event(app, week=0)
    # Вторая встреча через 2 дня: обе остаются в 7-дневном окне исправления явки
    second = (
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=FIRST_START + timedelta(days=2)
        )
    ).event_id
    reg_a = await join(app, first, 1)
    reg_b = await join(app, second, 1)

    await advance_to_after_start(app, first)
    await app.services.attendance.mark(OWNER_ID, first, {reg_a})
    await advance_to_after_start(app, second)
    await app.services.attendance.mark(OWNER_ID, second, {reg_b})
    ban = await active_ban(app, person(1).id)
    assert ban is not None and ban.event_id == second

    outcome = await app.services.attendance.correct(OWNER_ID, first, reg_a, Attendance.ATTENDED)

    assert outcome.unbanned
    assert await active_ban(app, person(1).id) is None


async def test_ручной_бан_не_снимается_исправлением_явки(app: App) -> None:
    event_id = await make_event(app)
    reg = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(OWNER_ID, event_id, {reg})
    await app.services.bans.unban(OWNER_ID, person(1).id)
    await app.services.bans.ban(OWNER_ID, person(1).id, comment="хамил")

    await app.services.attendance.correct(OWNER_ID, event_id, reg, Attendance.ATTENDED)

    ban = await active_ban(app, person(1).id)
    assert ban is not None and ban.reason == "manual"


async def test_сбой_пересчёта_бана_дожимается_повторной_попыткой(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await make_event(app)
    reg = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)

    original = app.services.bans.recalc_for_attendance

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("сбой БД")

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", broken)
    result = await app.services.attendance.mark(OWNER_ID, event_id, {reg})
    assert result.failed_ban_user_ids == (person(1).id,)
    assert await active_ban(app, person(1).id) is None

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", original)
    retried = await app.services.attendance.retry_pending_bans()

    assert retried == [event_id]
    assert await active_ban(app, person(1).id) is not None
    # Повторная попытка ничего не делает: встреча уже обработана
    assert await app.services.attendance.retry_pending_bans() == []


async def test_успешная_отметка_не_оставляет_работы_для_повторной_попытки(app: App) -> None:
    event_id = await make_event(app)
    reg = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    await app.services.attendance.mark(OWNER_ID, event_id, {reg})

    assert await app.services.attendance.retry_pending_bans() == []


async def test_планировщик_дожимает_пересчёт_банов(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await make_event(app)
    reg = await join(app, event_id, 1)
    await advance_to_after_start(app, event_id)
    original = app.services.bans.recalc_for_attendance

    async def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("сбой")

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", broken)
    await app.services.attendance.mark(OWNER_ID, event_id, {reg})
    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", original)

    report = await app.services.scheduler.tick()

    assert report.get("ban_recheck") == 1
    assert await active_ban(app, person(1).id) is not None


async def test_ручной_бан_сохраняет_имя_человека_который_не_писал_боту(app: App) -> None:
    stranger = UserInfo(5555, "Борис", "Петров", "boris")
    await app.services.bans.ban(OWNER_ID, stranger.id, comment="спам", user=stranger)

    async with app.sessions() as session:
        user = await session.get(User, stranger.id)
    assert user is not None
    assert (user.first_name, user.last_name, user.username) == ("Борис", "Петров", "boris")


# --- Запись ------------------------------------------------------------------------------


async def test_гость_при_выключенной_настройке_доменная_ошибка(app: App) -> None:
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as error:
        await app.services.registration.register(event_id, person(1), guests=1)
    assert error.value.code == "plus_one_disabled"


async def test_недопустимое_число_гостей_доменная_ошибка(app: App) -> None:
    await set_rule(app, "plus_one_enabled", True)
    event_id = await make_event(app)
    with pytest.raises(InvalidOperation) as error:
        await app.services.registration.register(event_id, person(1), guests=5)
    assert error.value.code == "invalid_guests"


async def test_пустое_имя_не_затирает_известное(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, UserInfo(42, "Анна", "Ли", "anna"))

    async with app.sessions() as session, session.begin():
        await users_repo.upsert(session, UserInfo(42, ""), app.clock.now())

    async with app.sessions() as session:
        user = await session.get(User, 42)
    assert user is not None and user.first_name == "Анна"


async def test_служебное_сообщение_об_отмене_с_именем_и_датой(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    app.notifier.service.clear()

    result = await app.services.registration.cancel(event_id, person(1).id)

    assert result.status is CancelStatus.OK
    message = app.notifier.service[-1]
    assert "Участник1" in message
    assert "24 октября" in message
    assert str(person(1).id) not in message


async def test_служебное_сообщение_о_заполнении_с_датой(app: App) -> None:
    event_id = await make_event(app, capacity=1)
    app.notifier.service.clear()

    result = await app.services.registration.register(event_id, person(1))

    assert result.status is RegisterStatus.OK and result.filled_event
    assert any("24 октября" in text and "заполнена" in text for text in app.notifier.service)


async def test_имя_в_служебном_сообщении_экранируется(app: App) -> None:
    event_id = await make_event(app)
    hacker = UserInfo(66, "<b>Хакер</b>")
    await app.services.registration.register(event_id, hacker)
    app.notifier.service.clear()

    await app.services.registration.cancel(event_id, hacker.id)

    assert "&lt;b&gt;Хакер&lt;/b&gt;" in app.notifier.service[-1]


# --- Тексты уведомлений ------------------------------------------------------------------


async def test_продвижение_из_очереди_одинаковым_текстом_из_настроек_на_всех_путях(
    app: App,
) -> None:
    """Отмена участником, удаление админом и бан — все дают текст `text.promoted` с датой."""
    await set_rule(app, "waitlist_enabled", True)
    await set_rule(app, "text.promoted", "Место ваше: {weekday}, {date}, {time}")
    event_id = await make_event(app, capacity=1)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    await app.services.registration.register(event_id, person(3))
    await app.services.registration.register(event_id, person(4))
    await enable_dm(app, person(2).id, person(3).id, person(4).id)

    await app.services.registration.cancel(event_id, person(1).id)
    parts = await app.services.participants.list_participants(OWNER_ID, event_id)
    first_active = parts.active[0].registration_id
    await app.services.participants.admin_remove(OWNER_ID, event_id, first_active)
    await app.services.bans.ban(OWNER_ID, person(3).id)

    expected = "Место ваше: суббота, 24 октября, 17:00"
    assert app.notifier.dms_to(person(2).id) == [expected]
    assert app.notifier.dms_to(person(3).id) == [expected]
    assert app.notifier.dms_to(person(4).id) == [expected]


async def test_напоминание_берёт_текст_из_настроек(app: App) -> None:
    await set_rule(app, "text.reminder", "Скоро встреча: {date}, {time}. Отмена до {deadline}.")
    event_id = await make_event(app, week=1)
    await app.services.registration.register(event_id, person(1))
    await enable_dm(app, person(1).id)
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        reminder_at = event.reminder_at
    app.clock.advance(reminder_at - app.clock.now() + timedelta(minutes=1))

    await app.services.scheduler.tick()

    assert app.notifier.dms_to(person(1).id) == [
        "Скоро встреча: 31 октября, 17:00. Отмена до 30 октября, 20:00."
    ]


async def test_пустое_место_не_оставляет_висящих_знаков(app: App) -> None:
    text = app.services.settings.render_text_now(
        "text.reminder",
        weekday="суббота",
        date="24 октября",
        time="17:00",
        place="",
        deadline="23 октября, 20:00",
    )
    assert ", ." not in text
    assert ", ," not in text
    assert "  " not in text
    assert "суббота, 24 октября, 17:00." in text


async def test_отмена_встречи_без_причины_не_оставляет_пустых_хвостов(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    await enable_dm(app, person(1).id)

    await app.services.events.cancel_event(OWNER_ID, event_id)

    [text] = app.notifier.dms_to(person(1).id)
    assert text == "Встреча 24 октября отменена."


async def test_причина_отмены_экранируется_один_раз(app: App) -> None:
    event_id = await make_event(app)
    await app.services.registration.register(event_id, person(1))
    await enable_dm(app, person(1).id)

    await app.services.events.cancel_event(OWNER_ID, event_id, reason="дождь & <гроза>")

    [text] = app.notifier.dms_to(person(1).id)
    assert "дождь &amp; &lt;гроза&gt;" in text
    assert "&amp;amp;" not in text


async def test_перенос_встречи_сообщает_новое_время_и_место(app: App) -> None:
    created = await app.services.events.create_event_as_admin(OWNER_ID, place="Кафе «Луна»")
    await app.services.registration.register(created.event_id, person(1))
    await enable_dm(app, person(1).id)
    async with app.sessions() as session:
        event = await session.get(Event, created.event_id)
        assert event is not None
        new_start = event.starts_at + timedelta(days=7)

    await app.services.events.reschedule(OWNER_ID, created.event_id, new_start)

    [text] = app.notifier.dms_to(person(1).id)
    assert text == "Встреча перенесена: теперь суббота, 31 октября, 17:00, Кафе «Луна»."


async def test_список_неявок_в_базе_совпадает_с_отметкой(app: App) -> None:
    """Регрессия: отметка записывается всем активным, лишних записей не трогаем."""
    event_id = await make_event(app)
    first = await join(app, event_id, 1)
    second = await join(app, event_id, 2)
    await advance_to_after_start(app, event_id)

    await app.services.attendance.mark(OWNER_ID, event_id, {second})

    async with app.sessions() as session:
        from meetup_bot.db.models import Registration

        rows = dict(
            (
                await session.execute(
                    select(Registration.id, Registration.attendance).where(
                        Registration.event_id == event_id
                    )
                )
            ).all()
        )
    assert rows == {first: "attended", second: "no_show"}
