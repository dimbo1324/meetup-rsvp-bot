"""Сборка поста встречи из БД и текущих настроек."""

from __future__ import annotations

import dataclasses

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.renderer.post import PostData, RenderedPost, render_post
from meetup_bot.services.posts import load_post_data
from meetup_bot.services.settings import SettingsService


def render_as(data: PostData, has_photo: bool) -> RenderedPost:
    """Рендер под вид КОНКРЕТНОГО разосланного поста: лимит 1024 для фото, 4096 для текста."""
    return render_post(dataclasses.replace(data, has_photo=has_photo))


async def build_post(
    sessions: async_sessionmaker[AsyncSession], settings: SettingsService, event_id: int
) -> tuple[PostData, RenderedPost] | None:
    data = await load_post_data(
        sessions,
        event_id,
        timezone=settings.timezone_now().key,
        show_names=settings.get_now("show_names_in_post"),
        plus_one_enabled=settings.options_now().plus_one_enabled,
    )
    if data is None:
        return None
    return data, render_post(data)
