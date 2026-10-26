"""Загрузка данных для рендера поста встречи."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.renderer.post import Participant, PostData, RenderedPost, render_post
from meetup_bot.repositories import event_posts, events, registrations
from meetup_bot.services import access
from meetup_bot.services.defaults import DEFAULT_TIMEZONE
from meetup_bot.services.names import public_name
from meetup_bot.services.settings import SettingsService


async def load_post_data(
    sessions: async_sessionmaker[AsyncSession],
    event_id: int,
    *,
    timezone: str = DEFAULT_TIMEZONE,
    show_names: bool = False,
    plus_one_enabled: bool = False,
) -> PostData | None:
    async with sessions() as session:
        event = await events.get(session, event_id)
        if event is None:
            return None
        rows = await registrations.list_active_with_users(session, event_id)
        taken = await events.seats_taken(session, event_id)
        waiting = await registrations.count_waitlist(session, event_id)
        participants = [
            Participant(name=public_name(user, reg.manual_name), guests=reg.guests)
            for reg, user in rows
        ]
        return PostData(
            event_id=event.id,
            status=event.status,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
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
            waiting=waiting,
        )


async def register_post(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    event_id: int,
    inline_message_id: str,
    posted_by: int | None,
    has_photo: bool | None,
) -> bool:
    """Запоминает разосланный inline-пост; True, если он новый.

    Идемпотентно: `chosen_inline_result` и нажатие кнопки (ленивая регистрация) могут
    прийти оба. `has_photo=None` — вид неизвестен, берётся по текущему фото встречи.
    """
    async with sessions() as session, session.begin():
        _, created = await event_posts.register(
            session,
            event_id=event_id,
            inline_message_id=inline_message_id,
            posted_by=posted_by,
            now=clock.now(),
            has_photo=has_photo,
        )
        return created


class PostService:
    """Предпросмотр поста для админа: тот же рендер, что у боевого поста, по текущим настройкам."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], settings: SettingsService
    ) -> None:
        self._sessions = sessions
        self._settings = settings

    async def preview(self, actor_id: int, event_id: int) -> RenderedPost | None:
        async with self._sessions() as session:
            await access.require_admin(session, actor_id)
        data = await load_post_data(
            self._sessions,
            event_id,
            timezone=self._settings.timezone_now().key,
            show_names=self._settings.get_now("show_names_in_post"),
            plus_one_enabled=self._settings.options_now().plus_one_enabled,
        )
        return None if data is None else render_post(data)
