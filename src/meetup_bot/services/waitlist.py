"""Лист ожидания (раздел 5.5). ЗАГЛУШКА с итоговой сигнатурой: реализацию пишет агент вехи M9."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Event


@dataclass(frozen=True)
class Promotion:
    registration_id: int
    user_id: int | None
    late_promoted: bool


async def promote_waitlist(
    session: AsyncSession,
    event: Event,
    now: datetime,
    *,
    late_promotion_hours: int = 24,
) -> list[Promotion]:
    """Переводит людей из очереди в активные, пока хватает мест. Вызывать под блокировкой встречи
    и внутри уже открытой транзакции `session`; сама ничего не коммитит."""
    return []
