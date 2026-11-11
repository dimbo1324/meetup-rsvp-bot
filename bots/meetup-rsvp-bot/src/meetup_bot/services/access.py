"""Проверка прав в слое сервисов: вызывается в каждой админской операции (раздел 3)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import UNFINISHED_EVENT_STATUSES, Event
from meetup_bot.repositories import admins, events
from meetup_bot.services.errors import ErrorCode, InvalidOperation, NotFound, PermissionDenied


async def require_admin(session: AsyncSession, user_id: int | None) -> None:
    """Бросает PermissionDenied, если пользователь не админ и не владелец."""
    if user_id is None or not await admins.is_admin(session, user_id):
        raise PermissionDenied(f"пользователь {user_id} не админ")


async def require_owner(session: AsyncSession, user_id: int | None) -> None:
    if user_id is None or not await admins.is_owner(session, user_id):
        raise PermissionDenied(f"пользователь {user_id} не владелец")


def event_not_found(event_id: int) -> NotFound:
    return NotFound(f"встреча {event_id} не найдена")


async def load_event_for_admin(
    session: AsyncSession, actor_id: int | None, event_id: int, *, editable_only: bool
) -> Event:
    """Права, существование и (при `editable_only`) незавершённость встречи — в одном месте.

    Права проверяются раньше поиска: постороннему не раскрываем, есть ли такая встреча.
    """
    await require_admin(session, actor_id)
    event = await events.get(session, event_id)
    if event is None:
        raise event_not_found(event_id)
    if editable_only and event.status not in UNFINISHED_EVENT_STATUSES:
        raise InvalidOperation(ErrorCode.BAD_STATUS)
    return event
