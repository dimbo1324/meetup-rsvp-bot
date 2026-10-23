"""Запросы «известных пользователей»: всех, кто когда-либо взаимодействовал с ботом."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import User
from meetup_bot.repositories.users import UserInfo


def _known_query(exclude_ids: Collection[int]) -> Select[Any]:
    # Пустое имя — заглушка из бана/синхронизации владельцев: выбирать такого бессмысленно
    query = select(User).where(User.first_name != "")
    if exclude_ids:
        query = query.where(User.id.not_in(list(exclude_ids)))
    return query


async def list_known(
    session: AsyncSession, limit: int, offset: int, exclude_ids: Collection[int] = ()
) -> list[User]:
    rows = await session.scalars(
        _known_query(exclude_ids)
        .order_by(User.updated_at.desc(), User.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows.all())


async def count_known(session: AsyncSession, exclude_ids: Collection[int] = ()) -> int:
    subquery = _known_query(exclude_ids).subquery()
    return int(await session.scalar(select(func.count()).select_from(subquery)) or 0)


async def get_many(session: AsyncSession, user_ids: Collection[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    rows = await session.scalars(select(User).where(User.id.in_(list(user_ids))))
    return {user.id: user for user in rows.all()}


async def remember(session: AsyncSession, info: UserInfo, now: datetime) -> None:
    """Сохраняет данные выбранного человека, только если его ещё нет или имя пустое.

    Существующую запись не перезаписываем: актуальные данные приходят от самого пользователя.
    """
    user = await session.get(User, info.id)
    if user is None:
        session.add(
            User(
                id=info.id,
                first_name=info.first_name,
                last_name=info.last_name,
                username=info.username,
                dm_available=False,
                created_at=now,
                updated_at=now,
            )
        )
    elif not user.first_name:
        user.first_name = info.first_name
        user.last_name = info.last_name
        user.username = info.username
