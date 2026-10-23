import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import FakeClock
from meetup_bot.db.models import AuditLog, Ban, BanReason, Event, EventStatus, User
from meetup_bot.repositories import events
from meetup_bot.services.events import EventService
from meetup_bot.services.registration import (
    CancelStatus,
    RegisterStatus,
    RegistrationOptions,
    RegistrationService,
    UserInfo,
)

Sessions = async_sessionmaker[AsyncSession]


def person(n: int) -> UserInfo:
    return UserInfo(id=1000 + n, first_name=f"Участник {n}", username=f"user{n}")


@pytest.fixture
def service(sessions: Sessions, clock: FakeClock) -> RegistrationService:
    return RegistrationService(sessions, clock)


@pytest.fixture
async def event_id(event_service: EventService) -> int:
    return await event_service.create_event(capacity=10, created_by=1)


async def taken(sessions: Sessions, event_id: int) -> int:
    async with sessions() as session:
        return await events.seats_taken(session, event_id)


async def test_одиннадцатый_получает_full(  # кейс 1
    service: RegistrationService, sessions: Sessions, event_id: int
) -> None:
    for n in range(10):
        assert (await service.register(event_id, person(n))).status is RegisterStatus.OK
    last = await service.register(event_id, person(10))
    assert last.status is RegisterStatus.FULL
    assert (last.seats_taken, last.capacity) == (10, 10)
    assert await taken(sessions, event_id) == 10


async def test_последняя_запись_помечает_заполнение(
    service: RegistrationService, event_id: int
) -> None:
    results = [await service.register(event_id, person(n)) for n in range(10)]
    assert [r.filled_event for r in results] == [False] * 9 + [True]


async def test_30_параллельных_записей_на_10_мест(  # кейс 2
    service: RegistrationService, sessions: Sessions, event_id: int
) -> None:
    results = await asyncio.gather(*(service.register(event_id, person(n)) for n in range(30)))
    statuses = [r.status for r in results]
    assert statuses.count(RegisterStatus.OK) == 10
    assert statuses.count(RegisterStatus.FULL) == 20
    assert await taken(sessions, event_id) == 10


async def test_параллельность_на_уровне_БД_без_блокировки_сервиса(
    sessions: Sessions, clock: FakeClock, event_id: int
) -> None:
    # Несколько экземпляров сервиса (у каждого свои блокировки) → защищают BEGIN IMMEDIATE и индекс
    instances = [RegistrationService(sessions, clock) for _ in range(30)]
    results = await asyncio.gather(
        *(svc.register(event_id, person(n)) for n, svc in enumerate(instances))
    )
    assert [r.status for r in results].count(RegisterStatus.OK) == 10
    assert await taken(sessions, event_id) == 10


async def test_двойное_нажатие(  # кейс 3
    service: RegistrationService, sessions: Sessions, event_id: int
) -> None:
    assert (await service.register(event_id, person(1))).status is RegisterStatus.OK
    assert (await service.register(event_id, person(1))).status is RegisterStatus.ALREADY
    assert await taken(sessions, event_id) == 1


async def test_отмена_за_минуту_до_дедлайна_и_через_минуту_после(  # кейс 4
    service: RegistrationService, sessions: Sessions, clock: FakeClock, event_id: int
) -> None:
    async with sessions() as session:
        event = await events.get(session, event_id)
        assert event is not None
        deadline = event.cancel_deadline_at

    await service.register(event_id, person(1))
    await service.register(event_id, person(2))

    clock.advance(deadline - timedelta(minutes=1) - clock.now())
    assert (await service.cancel(event_id, 1001)).status is CancelStatus.OK

    clock.advance(timedelta(minutes=2))
    late = await service.cancel(event_id, 1002)
    assert late.status is CancelStatus.TOO_LATE
    assert await taken(sessions, event_id) == 1


async def test_отмена_освобождает_место_и_позволяет_записаться_снова(
    service: RegistrationService, event_id: int
) -> None:
    for n in range(10):
        await service.register(event_id, person(n))
    assert (await service.register(event_id, person(10))).status is RegisterStatus.FULL
    assert (await service.cancel(event_id, 1000)).status is CancelStatus.OK
    assert (await service.register(event_id, person(10))).status is RegisterStatus.OK
    assert (await service.register(event_id, person(0))).status is RegisterStatus.FULL


async def test_отмена_без_записи(service: RegistrationService, event_id: int) -> None:
    assert (await service.cancel(event_id, 1)).status is CancelStatus.NOT_REGISTERED


async def test_забаненный(  # кейс 5
    service: RegistrationService, sessions: Sessions, clock: FakeClock, event_id: int
) -> None:
    async with sessions() as session, session.begin():
        session.add(User(id=1001, first_name="Бан", created_at=clock.now(), updated_at=clock.now()))
        await session.flush()  # без relationship() порядок вставки по FK не гарантирован
        session.add(Ban(user_id=1001, reason=BanReason.MANUAL.value, banned_at=clock.now()))
    result = await service.register(event_id, person(1))
    assert result.status is RegisterStatus.BANNED
    assert await taken(sessions, event_id) == 0


async def test_запись_после_закрытия(  # кейс 6
    service: RegistrationService, sessions: Sessions, clock: FakeClock, event_id: int
) -> None:
    async with sessions() as session:
        event = await events.get(session, event_id)
        assert event is not None
        closes = event.registration_closes_at

    clock.advance(closes - clock.now())
    assert (await service.register(event_id, person(1))).status is RegisterStatus.CLOSED


async def test_запись_после_дедлайна_отмены_но_до_закрытия_помечается(
    service: RegistrationService, sessions: Sessions, clock: FakeClock, event_id: int
) -> None:
    async with sessions() as session:
        event = await events.get(session, event_id)
        assert event is not None
        deadline = event.cancel_deadline_at

    clock.advance(deadline - clock.now() + timedelta(minutes=1))
    result = await service.register(event_id, person(1))
    assert result.status is RegisterStatus.OK
    assert result.cancel_deadline_passed


async def test_закрытая_встреча_и_несуществующая(
    service: RegistrationService, sessions: Sessions, event_id: int
) -> None:
    async with sessions() as session, session.begin():
        event = await events.get(session, event_id)
        assert event is not None
        event.status = EventStatus.CLOSED.value
    assert (await service.register(event_id, person(1))).status is RegisterStatus.CLOSED
    assert (await service.register(9999, person(1))).status is RegisterStatus.NOT_FOUND
    assert (await service.cancel(9999, 1)).status is CancelStatus.NOT_FOUND


async def test_данные_пользователя_обновляются_при_записи(
    service: RegistrationService, sessions: Sessions, event_id: int
) -> None:
    await service.register(event_id, UserInfo(id=7, first_name="Старое", username="old"))
    await service.cancel(event_id, 7)
    await service.register(event_id, UserInfo(id=7, first_name="Новое", username="new"))
    async with sessions() as session:
        user = await session.get(User, 7)
        assert user is not None
        assert (user.first_name, user.username) == ("Новое", "new")


async def test_аудит_пишется(
    service: RegistrationService, sessions: Sessions, event_id: int
) -> None:
    await service.register(event_id, person(1))
    await service.cancel(event_id, 1001)
    async with sessions() as session:
        actions = (await session.scalars(select(AuditLog.action).order_by(AuditLog.id))).all()
    assert actions == ["event.create", "registration.create", "registration.cancel"]


async def test_гость_выключен_по_умолчанию(service: RegistrationService, event_id: int) -> None:
    with pytest.raises(ValueError):
        await service.register(event_id, person(1), guests=1)
    with pytest.raises(ValueError):
        await service.register(event_id, person(1), guests=2)


async def test_плюс_один_занимает_два_места_и_одно_место_не_хватает(
    sessions: Sessions, clock: FakeClock, event_id: int
) -> None:
    service = RegistrationService(sessions, clock, RegistrationOptions(plus_one_enabled=True))
    for n in range(9):
        await service.register(event_id, person(n))
    result = await service.register(event_id, person(20), guests=1)
    assert result.status is RegisterStatus.ONLY_ONE_SEAT
    assert (await service.register(event_id, person(21))).status is RegisterStatus.OK


async def test_лист_ожидания_если_включён(
    sessions: Sessions, clock: FakeClock, event_service: EventService
) -> None:
    event_id = await event_service.create_event(capacity=1)
    service = RegistrationService(sessions, clock, RegistrationOptions(waitlist_enabled=True))
    assert (await service.register(event_id, person(1))).status is RegisterStatus.OK
    first = await service.register(event_id, person(2))
    second = await service.register(event_id, person(3))
    assert (first.status, first.position) == (RegisterStatus.WAITLISTED, 1)
    assert (second.status, second.position) == (RegisterStatus.WAITLISTED, 2)
    again = await service.register(event_id, person(3))
    assert (again.status, again.position) == (RegisterStatus.ALREADY_WAITLIST, 2)
    # выход из очереди — без штрафа и не освобождает место
    left = await service.cancel(event_id, 1003)
    assert (left.status, left.was_waitlist) == (CancelStatus.OK, True)
    assert await taken(sessions, event_id) == 1


async def test_ни_одной_лишней_записи_в_бд(
    service: RegistrationService, sessions: Sessions, event_id: int
) -> None:
    await asyncio.gather(*(service.register(event_id, person(1)) for _ in range(5)))
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(Event))
        assert count == 1
    assert await taken(sessions, event_id) == 1
