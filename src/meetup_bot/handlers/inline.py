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

from meetup_bot.clock import Clock
from meetup_bot.container import Services
from meetup_bot.db.models import EventStatus
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import DomainError
from meetup_bot.services.posts import register_post
from meetup_bot.telegram.common import START_ABOUT, check_admin
from meetup_bot.telegram.keyboards import to_markup
from meetup_bot.telegram.post_updater import PostUpdater
from meetup_bot.telegram.posts import build_post
from meetup_bot.texts.render import format_interval

logger = logging.getLogger(__name__)

router = Router(name="inline")

RESULT_PREFIX = "event:"
# Фото-результат помечен в id: chosen_inline_result не говорит, каким был результат, а вид
# разосланного поста (с фото или текстовый) определяет лимит подписи и способ правки
RESULT_PHOTO_SUFFIX = ":photo"
# Telegram принимает не более 50 результатов в одном ответе
MAX_RESULTS = 50
RESULT_TITLE = "Встреча"
RESULT_DESCRIPTION = "Занято {taken} из {capacity} мест"
_NOT_ADMIN_BUTTON = InlineQueryResultsButton(
    text="Бот записи на встречи", start_parameter=START_ABOUT
)


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
            title = f"{RESULT_TITLE}: {format_interval(data.starts_at, data.ends_at, tz)}"
            description = RESULT_DESCRIPTION.format(taken=item.seats_taken, capacity=item.capacity)
            if photo_file_id and rendered.has_photo:
                results.append(
                    InlineQueryResultCachedPhoto(
                        id=f"{result_id}{RESULT_PHOTO_SUFFIX}",
                        photo_file_id=photo_file_id,
                        title=title,
                        description=description,
                        caption=rendered.text,
                        reply_markup=markup,
                    )
                )
                continue
            results.append(
                InlineQueryResultArticle(
                    id=result_id,
                    title=title,
                    description=description,
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
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    post_updater: PostUpdater,
) -> None:
    """Запоминает отправленный пост, чтобы потом его редактировать (по inline_message_id)."""
    inline_message_id = chosen.inline_message_id
    if not chosen.result_id.startswith(RESULT_PREFIX) or inline_message_id is None:
        # Без клавиатуры у сообщения Telegram не присылает inline_message_id
        logger.warning("chosen_inline_result без данных поста")
        return
    raw_id = chosen.result_id.removeprefix(RESULT_PREFIX)
    has_photo = raw_id.endswith(RESULT_PHOTO_SUFFIX)
    try:
        event_id = int(raw_id.removesuffix(RESULT_PHOTO_SUFFIX))
    except ValueError:
        logger.warning("chosen_inline_result с неизвестным id результата")
        return
    try:
        await register_post(
            sessions,
            clock,
            event_id=event_id,
            inline_message_id=inline_message_id,
            posted_by=user_info.id,
            has_photo=has_photo,
        )
    except Exception:
        logger.exception("не удалось зарегистрировать пост встречи %s", event_id)
        return
    # Данные поста могли измениться между выдачей списка и отправкой
    post_updater.schedule(event_id)
