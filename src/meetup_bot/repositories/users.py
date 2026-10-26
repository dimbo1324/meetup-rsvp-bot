"""Запросы к таблице пользователей."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import User
from meetup_bot.user_info import UserInfo


async def get(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def ensure_exists(session: AsyncSession, user_id: int, now: datetime) -> User:
    """Заглушка без имени для внешних ключей (бан по ID, владелец до первого /start).

    Настоящие данные подтянутся при первом взаимодействии: `upsert` заменит пустое имя.
    """
    user = await session.get(User, user_id)
    if user is None:
        user = User(
            id=user_id,
            first_name="",
            dm_available=False,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
    return user


async def upsert(session: AsyncSession, info: UserInfo, now: datetime) -> User:
    """Создаёт пользователя или обновляет имя; без изменений в БД ничего не пишется.

    Вызывается на каждое сообщение, поэтому лишняя запись (и сдвиг `updated_at`, по которому
    сортируется список известных пользователей) недопустима.
    """
    user = await session.get(User, info.id)
    if user is None:
        user = User(
            id=info.id,
            first_name=info.first_name,
            last_name=info.last_name,
            username=info.username,
            dm_available=False,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        return user
    # Пустое имя приходит от заглушек (бан по ID, выбор без имени): известное не затираем
    fresh = (info.first_name, info.last_name, info.username)
    if info.first_name and fresh != (user.first_name, user.last_name, user.username):
        user.first_name = info.first_name
        user.last_name = info.last_name
        user.username = info.username
        user.updated_at = now
        await session.flush()
    return user


async def set_dm_available(session: AsyncSession, user_id: int, value: bool, now: datetime) -> None:
    """Можно ли писать пользователю в личку: true после /start, false после блокировки бота."""
    user = await session.get(User, user_id)
    if user is not None and user.dm_available != value:
        user.dm_available = value
        user.updated_at = now
