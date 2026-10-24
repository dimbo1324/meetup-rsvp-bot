"""Загрузка данных для рендера поста встречи."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.renderer.post import Participant, PostData
from meetup_bot.repositories import events, registrations
from meetup_bot.services.names import public_name
from meetup_bot.services.timing import DEFAULT_TIMEZONE


async def load_post_data(
    sessions: async_sessionmaker[AsyncSession],
    event_id: int,
    *,
    timezone: str = DEFAULT_TIMEZONE,
    show_names: bool = True,
    plus_one_enabled: bool = False,
) -> PostData | None:
    async with sessions() as session:
        event = await events.get(session, event_id)
        if event is None:
            return None
        rows = await registrations.list_active_with_users(session, event_id)
        taken = await events.seats_taken(session, event_id)
        participants = [
            Participant(name=public_name(user, reg.manual_name), guests=reg.guests)
            for reg, user in rows
        ]
        return PostData(
            event_id=event.id,
            status=event.status,
            starts_at=event.starts_at,
            timezone=timezone,
            capacity=event.capacity,
            seats_taken=taken,
            participants=participants,
            description_html=event.description_html,
            place=event.place,
            place_url=event.place_url,
            cancel_reason=event.cancel_reason,
            has_photo=event.photo_file_id is not None,
            photo_file_id=event.photo_file_id,
            show_names=show_names,
            plus_one_enabled=plus_one_enabled,
        )
