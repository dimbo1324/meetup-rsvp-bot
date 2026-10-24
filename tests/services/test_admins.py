"""AdminService: синхронизация владельцев, добавление и удаление админов, аудит (раздел 3)."""

import pytest
from sqlalchemy import select

from meetup_bot.db.models import Admin, AuditLog, User
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import InvalidOperation, NotFound, PermissionDenied
from tests.helpers import ADMIN_ID, OWNER_ID, App

STRANGER_ID = 777


async def roles(app: App) -> dict[int, str]:
    async with app.sessions() as session:
        return dict((await session.execute(select(Admin.user_id, Admin.role))).all())


async def audit_rows(app: App, action: str) -> list[AuditLog]:
    async with app.sessions() as session:
        rows = await session.scalars(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
        )
        return list(rows.all())


# --- sync_owners ---


async def test_sync_owners_создаёт_нового_владельца_с_заглушкой_пользователя(app: App) -> None:
    await app.services.admins.sync_owners([OWNER_ID, 7])
    assert (await roles(app))[7] == "owner"
    async with app.sessions() as session:
        stub = await session.get(User, 7)
    assert stub is not None


async def test_sync_owners_пишет_аудит_для_нового_владельца(app: App) -> None:
    await app.services.admins.sync_owners([OWNER_ID, 7])
    targets = [row.target_user_id for row in await audit_rows(app, "admin.owner_sync")]
    assert 7 in targets


async def test_sync_owners_повторный_вызов_ничего_не_меняет(app: App) -> None:
    await app.services.admins.sync_owners([OWNER_ID, 7])
    before = len(await audit_rows(app, "admin.owner_sync"))
    await app.services.admins.sync_owners([OWNER_ID, 7])
    assert len(await audit_rows(app, "admin.owner_sync")) == before
    assert await roles(app) == {OWNER_ID: "owner", ADMIN_ID: "admin", 7: "owner"}


async def test_sync_owners_повышает_существующего_админа_до_владельца(app: App) -> None:
    await app.services.admins.sync_owners([OWNER_ID, ADMIN_ID])
    assert (await roles(app))[ADMIN_ID] == "owner"


async def test_sync_owners_понижает_убранного_владельца_до_админа(app: App) -> None:
    await app.services.admins.sync_owners([7])
    result = await roles(app)
    assert result[OWNER_ID] == "admin"
    assert result[7] == "owner"
    demoted = await audit_rows(app, "admin.owner_demote")
    assert [row.target_user_id for row in demoted] == [OWNER_ID]


async def test_sync_owners_не_удаляет_убранного_владельца(app: App) -> None:
    await app.services.admins.sync_owners([7])
    assert OWNER_ID in await roles(app)


async def test_sync_owners_пустой_список_понижает_всех_владельцев(app: App) -> None:
    await app.services.admins.sync_owners([])
    assert set((await roles(app)).values()) == {"admin"}


async def test_после_понижения_бывший_владелец_не_может_добавлять_админов(app: App) -> None:
    await app.services.admins.sync_owners([7])
    with pytest.raises(PermissionDenied):
        await app.services.admins.add_admin(OWNER_ID, UserInfo(STRANGER_ID, "Новый"))


# --- add_admin ---


async def test_владелец_добавляет_админа(app: App) -> None:
    await app.services.admins.add_admin(OWNER_ID, UserInfo(STRANGER_ID, "Иван", "Петров", "ivan"))
    async with app.sessions() as session:
        admin = await session.get(Admin, STRANGER_ID)
        user = await session.get(User, STRANGER_ID)
    assert admin is not None
    assert (admin.role, admin.added_by) == ("admin", OWNER_ID)
    assert user is not None
    assert (user.first_name, user.last_name, user.username) == ("Иван", "Петров", "ivan")


async def test_добавление_админа_пишет_аудит(app: App) -> None:
    await app.services.admins.add_admin(OWNER_ID, UserInfo(STRANGER_ID, "Иван"))
    [row] = [r for r in await audit_rows(app, "admin.add") if r.target_user_id == STRANGER_ID]
    assert row.actor_id == OWNER_ID


async def test_обычный_админ_не_может_добавлять_админов(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.admins.add_admin(ADMIN_ID, UserInfo(STRANGER_ID, "Иван"))
    assert STRANGER_ID not in await roles(app)


async def test_посторонний_не_может_добавлять_админов(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.admins.add_admin(STRANGER_ID, UserInfo(STRANGER_ID, "Я"))


async def test_повторное_добавление_админа_даёт_already_admin(app: App) -> None:
    with pytest.raises(InvalidOperation) as error:
        await app.services.admins.add_admin(OWNER_ID, UserInfo(ADMIN_ID, "Админ"))
    assert error.value.code == "already_admin"


async def test_добавление_владельца_как_админа_даёт_already_admin(app: App) -> None:
    with pytest.raises(InvalidOperation) as error:
        await app.services.admins.add_admin(OWNER_ID, UserInfo(OWNER_ID, "Владелец"))
    assert error.value.code == "already_admin"
    assert (await roles(app))[OWNER_ID] == "owner"


# --- remove_admin ---


async def test_владелец_удаляет_админа(app: App) -> None:
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    assert ADMIN_ID not in await roles(app)


async def test_удалённый_админ_теряет_доступ(app: App) -> None:
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    with pytest.raises(PermissionDenied):
        await app.services.admins.list_admins(ADMIN_ID)


async def test_удаление_админа_пишет_аудит(app: App) -> None:
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    [row] = await audit_rows(app, "admin.remove")
    assert (row.actor_id, row.target_user_id) == (OWNER_ID, ADMIN_ID)


async def test_обычный_админ_не_может_удалять_админов(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.admins.remove_admin(ADMIN_ID, ADMIN_ID)
    assert ADMIN_ID in await roles(app)


async def test_владельца_нельзя_удалить(app: App) -> None:
    with pytest.raises(InvalidOperation) as error:
        await app.services.admins.remove_admin(OWNER_ID, OWNER_ID)
    assert error.value.code == "cannot_remove_owner"
    assert (await roles(app))[OWNER_ID] == "owner"


async def test_удаление_не_админа_даёт_not_found(app: App) -> None:
    with pytest.raises(NotFound):
        await app.services.admins.remove_admin(OWNER_ID, STRANGER_ID)


# --- list_admins ---


async def test_владелец_видит_список_админов(app: App) -> None:
    entries = await app.services.admins.list_admins(OWNER_ID)
    assert [(e.user_id, e.role) for e in entries] == [(OWNER_ID, "owner"), (ADMIN_ID, "admin")]


async def test_админ_может_смотреть_список_админов(app: App) -> None:
    entries = await app.services.admins.list_admins(ADMIN_ID)
    assert {e.user_id for e in entries} == {OWNER_ID, ADMIN_ID}


async def test_в_списке_админов_есть_данные_пользователя(app: App) -> None:
    entries = await app.services.admins.list_admins(OWNER_ID)
    admin = next(e for e in entries if e.user_id == ADMIN_ID)
    assert (admin.first_name, admin.username) == ("Админ", "admin2")
    assert admin.added_at == app.clock.now()


async def test_посторонний_не_видит_список_админов(app: App) -> None:
    with pytest.raises(PermissionDenied):
        await app.services.admins.list_admins(STRANGER_ID)
