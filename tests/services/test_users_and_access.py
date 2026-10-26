"""UserService, репозиторий пользователей и общая загрузка встречи для админа."""

from __future__ import annotations

from datetime import timedelta

import pytest

from meetup_bot.db.models import User
from meetup_bot.repositories import users as users_repo
from meetup_bot.services import access
from meetup_bot.services.errors import InvalidOperation, NotFound, PermissionDenied
from meetup_bot.services.users import UserService
from meetup_bot.user_info import UserInfo
from tests.helpers import ADMIN_ID, OWNER_ID, App, person


async def test_upsert_без_изменений_ничего_не_пишет(app: App) -> None:
    info = person(1)
    async with app.sessions() as session, session.begin():
        await users_repo.upsert(session, info, app.clock.now())
    created_at = app.clock.now()

    app.clock.advance(timedelta(hours=3))
    async with app.sessions() as session, session.begin():
        await users_repo.upsert(session, info, app.clock.now())
        user = await session.get(User, info.id)
        assert user is not None
        assert user.updated_at == created_at
        assert not session.dirty


async def test_upsert_с_новым_именем_обновляет_и_сдвигает_updated_at(app: App) -> None:
    async with app.sessions() as session, session.begin():
        await users_repo.upsert(session, person(1), app.clock.now())
    app.clock.advance(timedelta(hours=1))
    renamed = UserInfo(person(1).id, "Новое", None, "new_name")
    async with app.sessions() as session, session.begin():
        await users_repo.upsert(session, renamed, app.clock.now())
    async with app.sessions() as session:
        user = await session.get(User, person(1).id)
    assert user is not None
    assert (user.first_name, user.username, user.updated_at) == (
        "Новое",
        "new_name",
        app.clock.now(),
    )


async def test_upsert_с_пустым_именем_не_затирает_известное(app: App) -> None:
    async with app.sessions() as session, session.begin():
        await users_repo.upsert(session, person(1), app.clock.now())
        await users_repo.upsert(session, UserInfo(person(1).id, ""), app.clock.now())
        user = await session.get(User, person(1).id)
        assert user is not None
        assert user.first_name == person(1).first_name


async def test_ensure_exists_создаёт_заглушку_один_раз(app: App) -> None:
    async with app.sessions() as session, session.begin():
        first = await users_repo.ensure_exists(session, 4242, app.clock.now())
        again = await users_repo.ensure_exists(session, 4242, app.clock.now())
        assert first is again
        assert first.first_name == ""
    # Реальные данные заменяют заглушку
    async with app.sessions() as session, session.begin():
        user = await users_repo.upsert(session, UserInfo(4242, "Пётр"), app.clock.now())
        assert user.first_name == "Пётр"


def user_service(app: App) -> UserService:
    return UserService(app.sessions, app.clock)


async def test_is_admin_и_is_owner(app: App) -> None:
    service = user_service(app)
    assert await service.is_admin(OWNER_ID)
    assert await service.is_owner(OWNER_ID)
    assert await service.is_admin(ADMIN_ID)
    assert not await service.is_owner(ADMIN_ID)
    assert not await service.is_admin(person(1).id)


async def test_known_пропускает_заглушки_и_исключённых(app: App) -> None:
    service = user_service(app)
    for n in (1, 2, 3):
        await service.touch(person(n))
    async with app.sessions() as session, session.begin():
        await users_repo.ensure_exists(session, 9999, app.clock.now())

    everyone = await service.list_known(limit=50, offset=0)
    ids = {user.id for user in everyone}
    assert {person(1).id, person(2).id, person(3).id} <= ids
    assert 9999 not in ids
    assert await service.count_known() == len(everyone)

    without = await service.list_known(limit=50, offset=0, exclude_ids={person(1).id})
    assert person(1).id not in {user.id for user in without}
    assert await service.count_known({person(1).id}) == len(without)


async def test_list_known_страницы(app: App) -> None:
    service = user_service(app)
    for n in range(1, 6):
        app.clock.advance(timedelta(minutes=1))
        await service.touch(person(n))
    first = await service.list_known(limit=2, offset=0)
    second = await service.list_known(limit=2, offset=2)
    assert len({user.id for user in first + second}) == 4
    # Недавно активные первыми
    assert first[0].id == person(5).id


async def test_get_many_возвращает_только_известных(app: App) -> None:
    service = user_service(app)
    await service.touch(person(1))
    found = await service.get_many([person(1).id, 5555])
    assert set(found) == {person(1).id}
    assert found[person(1).id] == person(1)
    assert await service.get_many([]) == {}


async def test_load_event_for_admin_проверяет_права_наличие_и_статус(app: App) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID)).event_id
    async with app.sessions() as session:
        event = await access.load_event_for_admin(session, ADMIN_ID, event_id, editable_only=True)
        assert event.id == event_id
        with pytest.raises(PermissionDenied):
            await access.load_event_for_admin(session, 777, event_id, editable_only=True)
        with pytest.raises(NotFound):
            await access.load_event_for_admin(session, ADMIN_ID, 9999, editable_only=False)

    await app.services.events.cancel_event(OWNER_ID, event_id)
    async with app.sessions() as session:
        with pytest.raises(InvalidOperation) as error:
            await access.load_event_for_admin(session, ADMIN_ID, event_id, editable_only=True)
        assert error.value.code == "bad_status"
        # Для чтения статус не важен
        found = await access.load_event_for_admin(session, ADMIN_ID, event_id, editable_only=False)
        assert found.id == event_id
