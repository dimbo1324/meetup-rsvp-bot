"""Inline-режим: админ выбирает встречу и рассылает пост в любой чат (раздел 8.1)."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import (
    ChosenInlineResult,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InlineQueryResultsButton,
    InlineQueryResultUnion,
    InputTextMessageContent,
    LinkPreviewOptions,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.container import Services
from meetup_bot.db.models import EventStatus
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import DomainError
from meetup_bot.telegram.common import check_admin
from meetup_bot.telegram.keyboards import to_markup
from meetup_bot.telegram.post_updater import PostUpdater
from meetup_bot.telegram.posts import build_post
from meetup_bot.texts.render import format_moment

logger = logging.getLogger(__name__)

router = Router(name="inline")

RESULT_PREFIX = "event:"
# Telegram принимает не более 50 результатов в одном ответе
MAX_RESULTS = 50
_NOT_ADMIN_BUTTON = InlineQueryResultsButton(text="Бот записи на встречи", start_parameter="about")


@router.inline_query()
async def on_inline_query(
    query: InlineQuery,
    user_info: UserInfo,
    services: Services,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    # Права проверяем на каждый запрос: список встреч показываем только админам
    if not await check_admin(sessions, user_info.id):
        await query.answer([], cache_time=0, is_personal=True, button=_NOT_ADMIN_BUTTON)
        return

    results: list[InlineQueryResultUnion] = []
    try:
        items = await services.events.list_events(user_info.id)
        tz = services.settings.timezone_now()
        for item in items:
            if item.status != EventStatus.OPEN.value or len(results) >= MAX_RESULTS:
                continue
            built = await build_post(sessions, services.settings, item.id)
            if built is None:
                continue
            data, rendered = built
            markup = to_markup(rendered)
            result_id = f"{RESULT_PREFIX}{item.id}"
            photo_file_id = data.photo_file_id
            if photo_file_id and rendered.has_photo:
                results.append(
                    InlineQueryResultCachedPhoto(
                        id=result_id,
                        photo_file_id=photo_file_id,
                        caption=rendered.text,
                        reply_markup=markup,
                    )
                )
                continue
            when = format_moment(item.starts_at, tz)
            results.append(
                InlineQueryResultArticle(
                    id=result_id,
                    title=f"Встреча: {when}",
                    description=f"Занято {item.seats_taken} из {item.capacity} мест",
                    input_message_content=InputTextMessageContent(
                        message_text=rendered.text,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    ),
                    reply_markup=markup,
                )
            )
    except DomainError:
        logger.warning("отказ при выдаче inline-списка пользователю %s", user_info.id)
    # Без cache_time=0 Telegram показал бы устаревший список (число мест меняется постоянно)
    await query.answer(results, cache_time=0, is_personal=True)


@router.chosen_inline_result()
async def on_chosen_inline_result(
    chosen: ChosenInlineResult,
    user_info: UserInfo,
    services: Services,
    post_updater: PostUpdater,
) -> None:
    """Запоминает отправленный пост, чтобы потом его редактировать (по inline_message_id)."""
    inline_message_id = chosen.inline_message_id
    if not chosen.result_id.startswith(RESULT_PREFIX) or inline_message_id is None:
        # Без клавиатуры у сообщения Telegram не присылает inline_message_id
        logger.warning("chosen_inline_result без данных поста")
        return
    try:
        event_id = int(chosen.result_id.removeprefix(RESULT_PREFIX))
    except ValueError:
        logger.warning("chosen_inline_result с неизвестным id результата")
        return
    try:
        await services.users.register_post(event_id, inline_message_id, user_info.id)
    except Exception:
        logger.exception("не удалось зарегистрировать пост встречи %s", event_id)
        return
    # Данные поста могли измениться между выдачей списка и отправкой
    post_updater.schedule(event_id)
