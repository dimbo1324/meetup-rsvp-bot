"""Запросы к таблице пользователей."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import User


@dataclass(frozen=True)
class UserInfo:
    """Данные пользователя Telegram, актуальные на момент взаимодействия."""

    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None


async def upsert(session: AsyncSession, info: UserInfo, now: datetime) -> User:
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
    else:
        user.first_name = info.first_name
        user.last_name = info.last_name
        user.username = info.username
        user.updated_at = now
    await session.flush()
    return user
