"""Лист ожидания (раздел 5.5): продвижение очереди в активные записи."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Event, EventStatus, RegistrationStatus
from meetup_bot.repositories import audit, bans, events, registrations

__all__ = ["Promotion", "promote_waitlist"]


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
    и внутри уже открытой транзакции `session`; сама ничего не коммитит.

    Очередь обходится целиком, а не до первого непоместившегося: «+1», которому нужно два
    места, пропускается и сохраняет свою позицию, а следующий одиночный занимает свободное
    место (5.5). Места считаются заново из БД под той же блокировкой, поэтому лимит не
    превышается, а повторный вызов никого не продвинет дважды.
    """
    if event.status != EventStatus.OPEN.value:
        return []

    free = event.capacity - await events.seats_taken(session, event.id)
    if free <= 0:
        return []

    late = (event.starts_at - now) < timedelta(hours=late_promotion_hours)
    promoted: list[Promotion] = []
    for registration in await registrations.list_waitlist(session, event.id):
        if free <= 0:
            break
        need = 1 + registration.guests
        if need > free:
            continue
        # Забаненный не должен получить место; бан обычно уже снял его записи
        if registration.user_id is not None and await bans.is_banned(session, registration.user_id):
            continue
        registration.status = RegistrationStatus.ACTIVE.value
        registration.promoted_at = now
        registration.late_promoted = late
        free -= need
        promoted.append(Promotion(registration.id, registration.user_id, late))
        await audit.add(
            session,
            action="registration.promote",
            now=now,
            actor_id=None,
            event_id=event.id,
            target_user_id=registration.user_id,
            payload={"guests": registration.guests, "late_promoted": late},
        )
    if promoted:
        await session.flush()
    return promoted
