"""Запись без автозакрытия (`registration_close_hours_before = 0`): закрытие совпадает с началом."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from meetup_bot.db.models import AuditLog, Event, EventStatus
from meetup_bot.services.errors import InvalidOperation
from meetup_bot.services.registration import RegisterStatus
from tests.helpers import OWNER_ID, App, person

STARTS_AT = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)


async def create_event(app: App) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID)).event_id


async def get_event(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        return event


def go_to(app: App, moment: datetime) -> datetime:
    app.clock.advance(moment - app.clock.now())
    return app.clock.now()


async def test_закрытие_записи_совпадает_с_началом_а_дедлайн_отмены_прежний(app: App) -> None:
    event = await get_event(app, await create_event(app))
    assert event.registration_closes_at == event.starts_at == STARTS_AT
    # пятница 20:00 по Монтевидео
    assert event.cancel_deadline_at == datetime(2026, 10, 23, 23, 0, tzinfo=UTC)


async def test_за_секунду_до_начала_запись_принимается(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, STARTS_AT - timedelta(seconds=1))
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.OK


async def test_в_момент_начала_запись_закрыта(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, STARTS_AT)
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.CLOSED


async def test_планировщик_закрывает_и_завершает_в_момент_начала_без_двойной_работы(
    app: App,
) -> None:
    event_id = await create_event(app)
    await app.services.registration.register(event_id, person(1))
    now = go_to(app, STARTS_AT)
    lifecycle = app.services.lifecycle

    assert await lifecycle.close_due(now) == [event_id]
    assert await lifecycle.finish_due(now) == [event_id]
    assert await lifecycle.close_due(now) == []
    assert await lifecycle.finish_due(now) == []

    assert (await get_event(app, event_id)).status == EventStatus.FINISHED.value
    async with app.sessions() as session:
        actions = list((await session.scalars(select(AuditLog.action).order_by(AuditLog.id))).all())
    assert actions.count("event.close") == 1
    assert actions.count("event.finish") == 1
    # Встреча уже началась: итог записи в служебный чат не нужен
    assert app.notifier.service == []


async def test_планировщик_до_начала_ничего_не_делает(app: App) -> None:
    event_id = await create_event(app)
    now = go_to(app, STARTS_AT - timedelta(seconds=1))
    assert await app.services.lifecycle.close_due(now) == []
    assert await app.services.lifecycle.finish_due(now) == []
    assert (await get_event(app, event_id)).status == EventStatus.OPEN.value


async def test_ручное_закрытие_и_открытие_работают_до_начала(app: App) -> None:
    event_id = await create_event(app)
    events = app.services.events
    go_to(app, STARTS_AT - timedelta(minutes=1))
    await events.set_registration_open(OWNER_ID, event_id, False)
    assert (await get_event(app, event_id)).status == EventStatus.CLOSED.value
    closed = await app.services.registration.register(event_id, person(1))
    assert closed.status is RegisterStatus.CLOSED

    await events.set_registration_open(OWNER_ID, event_id, True)
    assert (await get_event(app, event_id)).status == EventStatus.OPEN.value
    reopened = await app.services.registration.register(event_id, person(1))
    assert reopened.status is RegisterStatus.OK


async def test_ручное_открытие_в_момент_начала_отклоняется(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, STARTS_AT - timedelta(minutes=1))
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    go_to(app, STARTS_AT)
    with pytest.raises(InvalidOperation, match="already_started"):
        await app.services.events.set_registration_open(OWNER_ID, event_id, True)
