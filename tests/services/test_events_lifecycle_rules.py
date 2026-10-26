"""Поздно созданная встреча, запрет правок после явки, отмена с пересчётом банов, карточка."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from meetup_bot.db.models import Ban, Event, EventStatus, Registration
from meetup_bot.repositories import events
from meetup_bot.services.errors import InvalidOperation, PermissionDenied
from meetup_bot.services.events import EventService
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

STRANGER = 777
# Пятница 23.10 16:30 в Монтевидео; фикстура clock — четверг 22.10 12:00 UTC
FRIDAY = datetime(2026, 10, 23, 19, 30, tzinfo=UTC)
NEXT_FRIDAY = FRIDAY + timedelta(weeks=1)
# Дедлайн отмены по умолчанию — четверг 20:00 по Монтевидео
DEADLINE = datetime(2026, 10, 22, 23, 0, tzinfo=UTC)


async def load(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await events.get(session, event_id)
        assert event is not None
        return event


async def create(app: App, **kwargs: object) -> int:
    created = await app.services.events.create_event_as_admin(
        OWNER_ID,
        starts_at=FRIDAY,
        **kwargs,  # type: ignore[arg-type]
    )
    return created.event_id


def after_deadline(app: App) -> None:
    app.clock.advance(DEADLINE - app.clock.now() + timedelta(minutes=30))


async def mark_attendance_in_db(app: App, event_id: int) -> None:
    """Встреча с уже отмеченной явкой в статусе open: старые данные или гонка."""
    async with app.sessions() as session, session.begin():
        event = await session.get(Event, event_id)
        assert event is not None
        event.attendance_marked_at = app.clock.now()
        event.attendance_marked_by = ADMIN_ID


async def finish(app: App, event_id: int) -> None:
    app.clock.advance((await load(app, event_id)).registration_closes_at - app.clock.now())
    await app.services.lifecycle.finish_due(app.clock.now())


# ---- поздно созданная встреча ----


async def test_поздно_созданная_встреча_зажимает_дедлайн_отмены(app: App) -> None:
    after_deadline(app)
    created = await app.services.events.create_event_as_admin(OWNER_ID, starts_at=FRIDAY)
    assert created.deadline_clamped
    event = await load(app, created.event_id)
    assert event.cancel_deadline_at == app.clock.now()
    assert event.created_at <= event.cancel_deadline_at


async def test_вовремя_созданная_встреча_дедлайн_не_зажимает(app: App) -> None:
    created = await app.services.events.create_event_as_admin(OWNER_ID, starts_at=FRIDAY)
    assert not created.deadline_clamped
    assert (await load(app, created.event_id)).cancel_deadline_at == DEADLINE


async def test_системное_создание_поздней_встречи_тоже_зажимает_дедлайн(app: App) -> None:
    after_deadline(app)
    event_id = await app.services.events.create_event(starts_at=FRIDAY)
    assert (await load(app, event_id)).cancel_deadline_at == app.clock.now()


async def test_запись_на_поздно_созданную_встречу_не_отменяется_без_штрафа(app: App) -> None:
    after_deadline(app)
    event_id = await create(app)
    await app.services.registration.register(event_id, person(1))
    result = await app.services.registration.cancel(event_id, person(1).id)
    assert result.status.value == "too_late"


async def test_перенос_после_дедлайна_зажимает_дедлайн_и_сообщает_об_этом(app: App) -> None:
    event_id = await create(app)
    after_deadline(app)
    # Новое начало — завтра днём: расчётный дедлайн (сегодня 20:00 по Монтевидео) позади
    tomorrow = app.clock.now() + timedelta(hours=20)
    result = await app.services.events.reschedule(OWNER_ID, event_id, tomorrow)
    assert result.deadline_clamped
    assert (await load(app, event_id)).cancel_deadline_at == app.clock.now()


async def test_перенос_на_далёкую_дату_дедлайн_не_зажимает(app: App) -> None:
    event_id = await create(app)
    result = await app.services.events.reschedule(OWNER_ID, event_id, NEXT_FRIDAY)
    assert not result.deadline_clamped


# ---- правки после отметки явки ----


async def test_перенос_после_отметки_явки_запрещён(app: App) -> None:
    event_id = await create(app)
    await mark_attendance_in_db(app, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.events.reschedule(OWNER_ID, event_id, NEXT_FRIDAY)
    assert error.value.code == "attendance_already_marked"
    assert (await load(app, event_id)).starts_at == FRIDAY


async def test_повторное_открытие_записи_после_отметки_явки_запрещено(app: App) -> None:
    event_id = await create(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    await mark_attendance_in_db(app, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.events.set_registration_open(OWNER_ID, event_id, True)
    assert error.value.code == "attendance_already_marked"
    assert (await load(app, event_id)).status == EventStatus.CLOSED.value


async def test_закрыть_запись_после_отметки_явки_можно(app: App) -> None:
    event_id = await create(app)
    await mark_attendance_in_db(app, event_id)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    assert (await load(app, event_id)).status == EventStatus.CLOSED.value


# ---- отмена встречи с отмеченной явкой ----


def events_with_bans(app: App) -> EventService:
    """Сервис встреч с пересчётом банов: в контейнере он подключается при сборке."""
    return EventService(
        app.sessions,
        app.clock,
        locks=app.services.locks,
        post_refresher=app.refresher,
        ban_service=app.services.bans,
    )


async def finish_with_no_show(app: App, event_id: int, n: int) -> None:
    await app.services.registration.register(event_id, person(n))
    await finish(app, event_id)
    async with app.sessions() as session:
        registration_id = await session.scalar(
            select(Registration.id).where(Registration.user_id == person(n).id)
        )
    assert registration_id is not None
    await app.services.attendance.mark(ADMIN_ID, event_id, {registration_id})


async def active_bans(app: App) -> int:
    async with app.sessions() as session:
        return len((await session.scalars(select(Ban.id).where(Ban.active.is_(True)))).all())


async def test_отмена_после_отметки_явки_снимает_автобан_по_неявке(app: App) -> None:
    event_id = await create(app)
    await finish_with_no_show(app, event_id, 1)
    assert await active_bans(app) == 1

    await events_with_bans(app).cancel_event(OWNER_ID, event_id, "не состоялась")

    assert await active_bans(app) == 0
    assert (await load(app, event_id)).status == EventStatus.CANCELLED.value


async def test_отмена_без_отметки_явки_банов_не_трогает(app: App) -> None:
    event_id = await create(app)
    await app.services.registration.register(event_id, person(1))
    await events_with_bans(app).cancel_event(OWNER_ID, event_id)
    assert await active_bans(app) == 0


async def test_сбой_пересчёта_после_отмены_не_отменяет_отмену(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await create(app)
    await finish_with_no_show(app, event_id, 1)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("сбой пересчёта")

    monkeypatch.setattr(app.services.bans, "recalc_for_attendance", boom)
    await events_with_bans(app).cancel_event(OWNER_ID, event_id)
    assert (await load(app, event_id)).status == EventStatus.CANCELLED.value


async def test_повторная_отмена_отклоняется(app: App) -> None:
    event_id = await create(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    with pytest.raises(InvalidOperation) as error:
        await app.services.events.cancel_event(OWNER_ID, event_id)
    assert error.value.code == "bad_status"


# ---- карточка и подтверждение уменьшения лимита ----


async def test_карточка_открытой_встречи(app: App) -> None:
    event_id = await create(app, capacity=1, place="Кафе")
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))  # очередь

    card = await app.services.events.get_card(ADMIN_ID, event_id)

    assert (card.status, card.capacity, card.seats_taken, card.waitlist_count) == ("open", 1, 1, 1)
    assert (card.place, card.starts_at) == ("Кафе", FRIDAY)
    assert (card.editable, card.can_close, card.can_open, card.can_cancel) == (
        True,
        True,
        False,
        True,
    )
    assert (card.can_reschedule, card.can_mark_attendance, card.attendance_marked) == (
        True,
        False,
        False,
    )


async def test_карточка_закрытой_встречи_разрешает_открытие(app: App) -> None:
    event_id = await create(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    card = await app.services.events.get_card(OWNER_ID, event_id)
    assert (card.can_close, card.can_open) == (False, True)


async def test_карточка_завершённой_встречи_даёт_отметку_явки_в_окне(app: App) -> None:
    event_id = await create(app)
    await finish(app, event_id)
    card = await app.services.events.get_card(OWNER_ID, event_id)
    assert (card.editable, card.can_mark_attendance, card.can_reschedule) == (False, True, False)
    assert card.can_cancel


async def test_карточка_отменённой_встречи_без_действий(app: App) -> None:
    event_id = await create(app)
    await app.services.events.cancel_event(OWNER_ID, event_id, "дождь")
    card = await app.services.events.get_card(OWNER_ID, event_id)
    assert card.cancel_reason == "дождь"
    assert not any(
        (card.can_close, card.can_open, card.can_cancel, card.can_reschedule, card.editable)
    )


async def test_карточка_только_для_админа(app: App) -> None:
    event_id = await create(app)
    with pytest.raises(PermissionDenied):
        await app.services.events.get_card(STRANGER, event_id)


@pytest.mark.parametrize(
    ("new_capacity", "expected"),
    [(10, False), (11, False), (5, True), (3, True), (2, False)],
)
async def test_подтверждение_нужно_только_при_допустимом_уменьшении_лимита(
    app: App, new_capacity: int, expected: bool
) -> None:
    event_id = await create(app, capacity=10)
    for n in (1, 2, 3):
        await app.services.registration.register(event_id, person(n))
    needed = await app.services.events.capacity_needs_confirmation(ADMIN_ID, event_id, new_capacity)
    assert needed is expected


async def test_подтверждение_лимита_проверяет_права_и_значение(app: App) -> None:
    event_id = await create(app)
    with pytest.raises(PermissionDenied):
        await app.services.events.capacity_needs_confirmation(STRANGER, event_id, 5)
    with pytest.raises(InvalidOperation):
        await app.services.events.capacity_needs_confirmation(ADMIN_ID, event_id, 0)


async def test_отмена_встречи_снимает_автобан_за_отмену_как_неявку_до_отметки_явки(
    app: App,
) -> None:
    event_id = await create(app)
    await app.services.registration.register(event_id, person(1))
    after_deadline(app)
    async with app.sessions() as session:
        registration_id = await session.scalar(
            select(Registration.id).where(Registration.user_id == person(1).id)
        )
    assert registration_id is not None
    await app.services.participants.admin_cancel_after_deadline(
        ADMIN_ID, event_id, registration_id, as_no_show=True
    )
    assert await active_bans(app) == 1

    await events_with_bans(app).cancel_event(OWNER_ID, event_id)

    # Неявки отменённой встречи не считаются, даже если явку по ней не отмечали
    assert await active_bans(app) == 0
