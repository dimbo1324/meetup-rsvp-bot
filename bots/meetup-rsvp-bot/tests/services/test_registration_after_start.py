"""Запись после начала (решение заказчика 2026-10-25): опоздавшие записываются, пока есть места.

Встреча по умолчанию — пятница 16:30–18:30 в Монтевидео; запись закрывается через 60 минут
после начала (17:30) или вручную админом в любой момент; явка запрашивается в 18:00.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from meetup_bot.db.models import AuditLog, Event, EventStatus, Registration
from meetup_bot.services.errors import InvalidOperation
from meetup_bot.services.registration import CancelStatus, RegisterStatus
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

STARTS_AT = datetime(2026, 10, 23, 19, 30, tzinfo=UTC)  # 16:30 по Монтевидео
CLOSES_AT = datetime(2026, 10, 23, 20, 30, tzinfo=UTC)  # 17:30
PROMPT_AT = datetime(2026, 10, 23, 21, 0, tzinfo=UTC)  # 18:00


async def create_event(app: App, capacity: int = 10) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)).event_id


async def get_event(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        return event


def go_to(app: App, moment: datetime) -> datetime:
    app.clock.advance(moment - app.clock.now())
    return app.clock.now()


def local(hour: int, minute: int) -> datetime:
    """Момент пятницы 23.10 по Монтевидео (UTC−3) в UTC."""
    return datetime(2026, 10, 23, hour + 3, minute, tzinfo=UTC)


async def audit_actions(app: App, event_id: int) -> list[str]:
    async with app.sessions() as session:
        rows = await session.scalars(
            select(AuditLog.action).where(AuditLog.event_id == event_id).order_by(AuditLog.id)
        )
        return list(rows.all())


async def test_моменты_новой_встречи_по_умолчанию(app: App) -> None:
    event = await get_event(app, await create_event(app))
    assert event.starts_at == STARTS_AT
    assert event.ends_at == local(18, 30)
    assert event.registration_closes_at == CLOSES_AT
    assert event.attendance_prompt_at == PROMPT_AT
    # Четверг 20:00 по Монтевидео
    assert event.cancel_deadline_at == datetime(2026, 10, 22, 23, 0, tzinfo=UTC)


async def test_запись_в_16_45_при_свободных_местах_принята(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, local(16, 45))
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.OK
    # После дедлайна отмены человек предупреждается, что отменить сам уже не сможет
    assert result.cancel_deadline_passed


async def test_за_секунду_до_закрытия_запись_принята(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, CLOSES_AT - timedelta(seconds=1))
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.OK


async def test_в_момент_закрытия_запись_отклоняется(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, CLOSES_AT)
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.CLOSED


async def test_запись_в_17_30_закрыта_даже_до_тика_планировщика(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, local(17, 30))
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.CLOSED
    assert (await get_event(app, event_id)).status == EventStatus.OPEN.value


async def test_после_начала_при_полной_встрече_человек_встаёт_в_очередь(app: App) -> None:
    event_id = await create_event(app, capacity=1)
    await app.services.registration.register(event_id, person(1))
    go_to(app, local(16, 45))
    result = await app.services.registration.register(event_id, person(2))
    assert result.status is RegisterStatus.WAITLISTED


async def test_отмена_после_дедлайна_по_прежнему_слишком_поздно(app: App) -> None:
    event_id = await create_event(app)
    await app.services.registration.register(event_id, person(1))
    go_to(app, local(16, 45))
    result = await app.services.registration.cancel(event_id, person(1).id)
    assert result.status is CancelStatus.TOO_LATE


async def test_выйти_из_очереди_можно_после_начала_до_закрытия(app: App) -> None:
    event_id = await create_event(app, capacity=1)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    go_to(app, CLOSES_AT - timedelta(seconds=1))
    result = await app.services.registration.cancel(event_id, person(2).id)
    assert result.status is CancelStatus.OK
    assert result.was_waitlist


async def test_выйти_из_очереди_после_закрытия_нельзя(app: App) -> None:
    event_id = await create_event(app, capacity=1)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    go_to(app, CLOSES_AT)
    result = await app.services.registration.cancel(event_id, person(2).id)
    assert result.status is CancelStatus.TOO_LATE


async def test_продвижение_из_очереди_после_начала_помечается_поздним(app: App) -> None:
    event_id = await create_event(app, capacity=1)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    go_to(app, local(16, 45))
    data = await app.services.participants.list_participants(OWNER_ID, event_id)
    await app.services.participants.admin_remove(OWNER_ID, event_id, data.active[0].registration_id)
    async with app.sessions() as session:
        promoted = await session.scalar(
            select(Registration).where(Registration.user_id == person(2).id)
        )
    assert promoted is not None
    assert promoted.status == "active"
    assert promoted.late_promoted


async def test_планировщик_в_17_30_закрывает_и_завершает_без_двойной_работы(app: App) -> None:
    event_id = await create_event(app)
    await app.services.registration.register(event_id, person(1))
    lifecycle = app.services.lifecycle

    # В момент начала встреча ещё открыта для опоздавших
    now = go_to(app, STARTS_AT)
    assert await lifecycle.close_due(now) == []
    assert await lifecycle.finish_due(now) == []
    assert (await get_event(app, event_id)).status == EventStatus.OPEN.value

    now = go_to(app, local(17, 30))
    assert await lifecycle.close_due(now) == [event_id]
    assert await lifecycle.finish_due(now) == [event_id]
    assert await lifecycle.close_due(now) == []
    assert await lifecycle.finish_due(now) == []
    later = now + timedelta(minutes=1)
    assert await lifecycle.close_due(later) == []
    assert await lifecycle.finish_due(later) == []

    assert (await get_event(app, event_id)).status == EventStatus.FINISHED.value
    actions = await audit_actions(app, event_id)
    assert actions.count("event.close") == 1
    assert actions.count("event.finish") == 1
    # Итог записи уходит в служебный чат ровно один раз, хотя встреча уже идёт
    assert len(app.notifier.service) == 1
    assert "закрыта" in app.notifier.service[0]
    assert "Заполнено 1 из 10." in app.notifier.service[0]


async def test_тик_планировщика_в_17_30_закрывает_и_завершает_одним_проходом(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, local(17, 30))
    await app.services.scheduler.tick()
    await app.services.scheduler.tick()
    assert (await get_event(app, event_id)).status == EventStatus.FINISHED.value
    actions = await audit_actions(app, event_id)
    assert (actions.count("event.close"), actions.count("event.finish")) == (1, 1)
    summaries = [text for text in app.notifier.service if "закрыта" in text]
    assert len(summaries) == 1


async def test_ручное_закрытие_в_16_50(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, local(16, 50))
    await app.services.events.set_registration_open(ADMIN_ID, event_id, False)
    event = await get_event(app, event_id)
    assert event.status == EventStatus.CLOSED.value
    assert event.closed_at == local(16, 50)
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.CLOSED
    # Встреча не завершается раньше времени закрытия записи
    assert await app.services.lifecycle.finish_due(app.clock.now()) == []


async def test_повторное_открытие_до_17_30_работает(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, local(16, 50))
    await app.services.events.set_registration_open(ADMIN_ID, event_id, False)
    go_to(app, CLOSES_AT - timedelta(seconds=1))
    await app.services.events.set_registration_open(ADMIN_ID, event_id, True)
    event = await get_event(app, event_id)
    assert event.status == EventStatus.OPEN.value
    # Время закрытия не подменяется: планировщик закроет запись в 17:30, как и было
    assert event.registration_closes_at == CLOSES_AT
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.OK


async def test_повторное_открытие_после_17_30_отклоняется(app: App) -> None:
    event_id = await create_event(app)
    go_to(app, local(16, 50))
    await app.services.events.set_registration_open(ADMIN_ID, event_id, False)
    go_to(app, CLOSES_AT)
    with pytest.raises(InvalidOperation) as exc:
        await app.services.events.set_registration_open(ADMIN_ID, event_id, True)
    assert exc.value.code == "registration_time_passed"
    assert (await get_event(app, event_id)).status == EventStatus.CLOSED.value
