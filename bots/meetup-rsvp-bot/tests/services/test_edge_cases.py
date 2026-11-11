"""Спорные и пограничные сценарии: гонки, часовые пояса, превышение лимита, баны с гостями."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select

from meetup_bot.db.models import Event, EventStatus, Registration, RegistrationStatus
from meetup_bot.repositories import events as events_repo
from meetup_bot.services.registration import RegisterStatus
from meetup_bot.services.timing import ScheduleSettings, compute_times, next_event_start
from tests.helpers import OWNER_ID, App, person
from tests.services.test_attendance import make_event, set_rule


async def _taken(app: App, event_id: int) -> int:
    async with app.sessions() as session:
        return await events_repo.seats_taken(session, event_id)


async def _active_count(app: App, event_id: int) -> int:
    async with app.sessions() as session:
        return int(
            await session.scalar(
                select(func.count()).where(
                    Registration.event_id == event_id,
                    Registration.status == RegistrationStatus.ACTIVE.value,
                )
            )
            or 0
        )


# --- гонки -----------------------------------------------------------------------------------


async def test_вперемешку_запись_и_отмена_лимит_не_превышается(app: App) -> None:
    event_id = await make_event(app, capacity=5)
    for n in range(5):
        await app.services.registration.register(event_id, person(n))

    jobs = [app.services.registration.register(event_id, person(100 + n)) for n in range(15)]
    jobs += [app.services.registration.cancel(event_id, person(n).id) for n in range(5)]
    await asyncio.gather(*jobs)

    taken = await _taken(app, event_id)
    assert taken <= 5
    assert taken == await _active_count(app, event_id)


async def test_бан_во_время_записи_не_оставляет_забаненного_участником(app: App) -> None:
    events = [await make_event(app, week=w) for w in range(3)]
    target = person(7)

    await asyncio.gather(
        *(app.services.registration.register(e, target) for e in events),
        app.services.bans.ban(OWNER_ID, target.id, comment="спам"),
    )

    async with app.sessions() as session:
        live = await session.scalar(
            select(func.count()).where(
                Registration.user_id == target.id,
                Registration.status == RegistrationStatus.ACTIVE.value,
            )
        )
    assert live == 0


async def test_отмена_встречи_параллельно_с_записью_ничего_не_ломает(app: App) -> None:
    event_id = await make_event(app, capacity=10)
    results = await asyncio.gather(
        *(app.services.registration.register(event_id, person(n)) for n in range(10)),
        app.services.events.cancel_event(OWNER_ID, event_id, "гроза"),
        return_exceptions=True,
    )
    assert not [r for r in results if isinstance(r, BaseException)]
    after = await app.services.registration.register(event_id, person(99))
    assert after.status is RegisterStatus.CLOSED
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
    assert event is not None and event.status == EventStatus.CANCELLED.value


# --- превышение лимита админом -----------------------------------------------------------------


async def test_после_добавления_сверх_лимита_отмена_не_продвигает_очередь(app: App) -> None:
    await set_rule(app, "waitlist_enabled", True)
    event_id = await make_event(app, capacity=2)
    await app.services.registration.register(event_id, person(1))
    await app.services.registration.register(event_id, person(2))
    await app.services.participants.admin_add(OWNER_ID, event_id, manual_name="Друг", force=True)
    waiting = await app.services.registration.register(event_id, person(3))
    assert waiting.status is RegisterStatus.WAITLISTED

    result = await app.services.registration.cancel(event_id, person(1).id)

    # Мест по-прежнему не больше лимита: 2 занято при лимите 2, очередь стоит
    assert result.promoted == ()
    assert await _taken(app, event_id) == 2


async def test_бан_участника_с_гостем_освобождает_два_места(app: App) -> None:
    await set_rule(app, "plus_one_enabled", True)
    await set_rule(app, "waitlist_enabled", True)
    event_id = await make_event(app, capacity=2)
    await app.services.registration.register(event_id, person(1), guests=1)
    await app.services.registration.register(event_id, person(2))
    await app.services.registration.register(event_id, person(3))

    await app.services.bans.ban(OWNER_ID, person(1).id)

    assert await _taken(app, event_id) == 2
    assert await _active_count(app, event_id) == 2


# --- часовые пояса ---------------------------------------------------------------------------


def test_переход_на_зимнее_время_не_сдвигает_время_встречи() -> None:
    """Мадрид: 25.10.2026 переход CEST→CET. Встреча в 17:00 по местному и до, и после."""
    madrid = ScheduleSettings(timezone="Europe/Madrid", event_weekday=5, event_time=time(17, 0))
    before = next_event_start(datetime(2026, 10, 20, 12, 0, tzinfo=UTC), madrid)
    after = next_event_start(datetime(2026, 10, 26, 12, 0, tzinfo=UTC), madrid)

    assert before == datetime(2026, 10, 24, 15, 0, tzinfo=UTC)  # UTC+2
    assert after == datetime(2026, 10, 31, 16, 0, tzinfo=UTC)  # UTC+1


def test_дедлайн_отмены_считается_по_местному_календарю_через_переход_времени() -> None:
    madrid = ScheduleSettings(timezone="Europe/Madrid")
    # Встреча в воскресенье 25.10 в 17:00 (уже зимнее время), дедлайн — суббота 20:00 летнего
    times = compute_times(datetime(2026, 10, 25, 16, 0, tzinfo=UTC), madrid)
    assert times.cancel_deadline_at == datetime(2026, 10, 24, 18, 0, tzinfo=UTC)


async def test_смена_часового_пояса_не_двигает_созданные_встречи(app: App) -> None:
    event_id = await make_event(app)
    before = (await _event(app, event_id)).starts_at
    await app.services.settings.set("timezone", "Europe/Moscow", OWNER_ID)
    assert (await _event(app, event_id)).starts_at == before


async def _event(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
    assert event is not None
    return event


# --- данные пользователя ------------------------------------------------------------------


async def test_имя_из_эмодзи_и_rtl_сохраняется_как_есть(app: App) -> None:
    from meetup_bot.user_info import UserInfo

    event_id = await make_event(app)
    weird = UserInfo(77, "🎉👩‍👩‍👧 مرحبا", "​Тест", None)
    result = await app.services.registration.register(event_id, weird)
    assert result.status is RegisterStatus.OK
    data = await app.services.participants.list_participants(OWNER_ID, event_id)
    assert data.active[0].name == "🎉👩‍👩‍👧 مرحبا ​Тест"


async def test_удалённая_встреча_в_прошлом_не_даёт_записаться(app: App) -> None:
    event_id = await make_event(app)
    app.clock.advance(timedelta(days=30))
    result = await app.services.registration.register(event_id, person(1))
    assert result.status is RegisterStatus.CLOSED
