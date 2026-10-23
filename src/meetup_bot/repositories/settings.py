"""Таблица settings: хранятся только значения, изменённые относительно умолчаний."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Setting


async def get_all(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(select(Setting.key, Setting.value))
    return {key: value for key, value in rows.all()}


async def upsert(
    session: AsyncSession, key: str, value: Any, *, actor_id: int | None, now: datetime
) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value, updated_by=actor_id, updated_at=now))
    else:
        row.value = value
        row.updated_by = actor_id
        row.updated_at = now
    await session.flush()


async def delete_key(session: AsyncSession, key: str) -> None:
    """Удаляет переопределение — настройка возвращается к значению по умолчанию."""
    await session.execute(delete(Setting).where(Setting.key == key))
