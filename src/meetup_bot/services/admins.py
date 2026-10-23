"""Управление админами: владельцы приходят из конфигурации, остальных добавляет владелец."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import AdminRole, User
from meetup_bot.repositories import admins, audit, users
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import InvalidOperation, NotFound


@dataclass(frozen=True)
class AdminEntry:
    user_id: int
    role: str
    first_name: str
    last_name: str | None
    username: str | None
    added_at: datetime


class AdminService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock

    async def sync_owners(self, owner_ids: Iterable[int]) -> None:
        """Приводит роли владельцев в соответствие с OWNER_IDS (вызывается при старте).

        Убранный из конфигурации владелец понижается до админа, а не удаляется: так он не
        теряет доступ внезапно из-за опечатки в окружении.
        """
        wanted = set(owner_ids)
        now = self._clock.now()
        async with self._sessions() as session, session.begin():
            for owner_id in sorted(wanted):
                # Заглушка нужна из-за внешнего ключа; настоящие данные подтянутся при /start
                if await session.get(User, owner_id) is None:
                    session.add(User(id=owner_id, created_at=now, updated_at=now))
                    await session.flush()
                admin = await admins.get(session, owner_id)
                if admin is None:
                    await admins.insert(
                        session, user_id=owner_id, role=AdminRole.OWNER, added_by=None, now=now
                    )
                    await audit.add(
                        session, action="admin.owner_sync", now=now, target_user_id=owner_id
                    )
                elif admin.role != AdminRole.OWNER.value:
                    admin.role = AdminRole.OWNER.value
                    await audit.add(
                        session, action="admin.owner_sync", now=now, target_user_id=owner_id
                    )
            for admin, _ in await admins.list_with_users(session):
                if admin.role == AdminRole.OWNER.value and admin.user_id not in wanted:
                    admin.role = AdminRole.ADMIN.value
                    await audit.add(
                        session,
                        action="admin.owner_demote",
                        now=now,
                        target_user_id=admin.user_id,
                    )

    async def add_admin(self, actor_id: int, user: UserInfo) -> None:
        now = self._clock.now()
        async with self._sessions() as session, session.begin():
            await admins.require_owner(session, actor_id)
            if await admins.get(session, user.id) is not None:
                raise InvalidOperation("already_admin")
            await users.upsert(session, user, now)
            await admins.insert(
                session, user_id=user.id, role=AdminRole.ADMIN, added_by=actor_id, now=now
            )
            await audit.add(
                session, action="admin.add", now=now, actor_id=actor_id, target_user_id=user.id
            )

    async def remove_admin(self, actor_id: int, user_id: int) -> None:
        now = self._clock.now()
        async with self._sessions() as session, session.begin():
            await admins.require_owner(session, actor_id)
            admin = await admins.get(session, user_id)
            if admin is None:
                raise NotFound(f"админ {user_id} не найден")
            if admin.role == AdminRole.OWNER.value:
                raise InvalidOperation("cannot_remove_owner")
            await admins.delete(session, admin)
            await audit.add(
                session, action="admin.remove", now=now, actor_id=actor_id, target_user_id=user_id
            )

    async def list_admins(self, actor_id: int) -> list[AdminEntry]:
        async with self._sessions() as session:
            await admins.require_admin(session, actor_id)
            return [
                AdminEntry(
                    user_id=admin.user_id,
                    role=admin.role,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    username=user.username,
                    added_at=admin.added_at,
                )
                for admin, user in await admins.list_with_users(session)
            ]
