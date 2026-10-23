"""Журнал аудита: разбор спорных ситуаций («я записывался!»)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import AuditLog


async def add(
    session: AsyncSession,
    *,
    action: str,
    now: datetime,
    actor_id: int | None = None,
    event_id: int | None = None,
    target_user_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            event_id=event_id,
            target_user_id=target_user_id,
            payload=payload or {},
            created_at=now,
        )
    )
