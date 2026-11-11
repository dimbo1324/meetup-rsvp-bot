"""Запросы к таблице постов встреч (`event_posts`): по одному на каждое inline-сообщение."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from meetup_bot.db.models import Event, EventPost, EventStatus


def _is_live() -> ColumnElement[bool]:
    return and_(EventPost.is_dead.is_(False), EventPost.inline_message_id.is_not(None))


async def get_by_inline_id(session: AsyncSession, inline_message_id: str) -> EventPost | None:
    return await session.scalar(
        select(EventPost).where(EventPost.inline_message_id == inline_message_id)
    )


async def register(
    session: AsyncSession,
    *,
    event_id: int,
    inline_message_id: str,
    posted_by: int | None,
    now: datetime,
    has_photo: bool | None = None,
) -> tuple[EventPost, bool]:
    """Регистрирует пост; идемпотентно. Возвращает (пост, создан_ли_сейчас).

    Вызывается и из `chosen_inline_result`, и при первом нажатии кнопки (lazy registration),
    поэтому повторная регистрация того же `inline_message_id` не создаёт дубль.
    `has_photo=None` — вид поста неизвестен, берётся по текущему фото встречи.
    """
    existing = await get_by_inline_id(session, inline_message_id)
    if existing is not None:
        if existing.is_dead:
            # Нажатие на кнопку доказывает, что сообщение живо: помеченное «мёртвым» ошибочно
            existing.is_dead = False
            existing.last_render_hash = None
        return existing, False
    event = await session.get(Event, event_id)
    photo = event.photo_file_id if event is not None else None
    if has_photo is None:
        has_photo = bool(photo)
    post = EventPost(
        event_id=event_id,
        inline_message_id=inline_message_id,
        posted_by=posted_by,
        created_at=now,
        has_photo=has_photo,
        last_photo_file_id=photo if has_photo else None,
    )
    session.add(post)
    await session.flush()
    return post, True


async def list_live(session: AsyncSession, event_id: int) -> list[EventPost]:
    """Живые посты встречи (не помеченные `is_dead`), в порядке создания."""
    rows = await session.scalars(
        select(EventPost).where(EventPost.event_id == event_id, _is_live()).order_by(EventPost.id)
    )
    return list(rows.all())


async def list_live_event_ids(session: AsyncSession, *, starts_after: datetime) -> list[int]:
    """Встречи с живыми постами, начавшиеся не раньше `starts_after`: их пересобирают после
    перезапуска. Посты давних встреч не трогаем: список растёт каждую неделю."""
    rows = await session.scalars(
        select(EventPost.event_id)
        .join(Event, EventPost.event_id == Event.id)
        .where(_is_live(), Event.starts_at >= starts_after)
        .distinct()
    )
    return list(rows.all())


async def list_open_without_live_posts(
    session: AsyncSession, *, starts_after: datetime
) -> list[Event]:
    """Открытые будущие встречи без живых постов и без напоминания «пора разослать пост»."""
    rows = await session.scalars(
        select(Event)
        .where(
            Event.status == EventStatus.OPEN.value,
            Event.starts_at > starts_after,
            Event.post_reminder_sent_at.is_(None),
            ~exists().where(EventPost.event_id == Event.id, _is_live()),
        )
        .order_by(Event.starts_at, Event.id)
    )
    return list(rows.all())


async def has_live(session: AsyncSession, event_id: int) -> bool:
    found = await session.scalar(
        select(EventPost.id).where(EventPost.event_id == event_id, _is_live()).limit(1)
    )
    return found is not None


async def mark_dead(session: AsyncSession, post_id: int) -> None:
    post = await session.get(EventPost, post_id)
    if post is not None:
        post.is_dead = True


async def mark_edited(
    session: AsyncSession,
    post_id: int,
    *,
    content_hash: str,
    now: datetime,
    has_photo: bool,
    photo_file_id: str | None,
) -> None:
    post = await session.get(EventPost, post_id)
    if post is not None:
        post.last_render_hash = content_hash
        post.last_edited_at = now
        post.has_photo = has_photo
        post.last_photo_file_id = photo_file_id
