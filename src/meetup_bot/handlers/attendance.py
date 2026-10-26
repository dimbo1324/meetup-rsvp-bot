"""Кнопки списка явки: переключатели ✅/🚫, страницы, «Готово» и исправление отметок."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.container import Services
from meetup_bot.services.attendance import AlreadyMarked
from meetup_bot.services.errors import DomainError
from meetup_bot.telegram.attendance_callbacks import (
    PAGE_PREFIX,
    TARGET_DONE,
    TARGET_NOOP,
    AttendanceCallback,
)
from meetup_bot.telegram.attendance_ui import AttendanceUI, has_done
from meetup_bot.telegram.common import check_admin, safe_answer
from meetup_bot.telegram.errors_text import ANOTHER_ADMIN, NO_ACCESS, describe_error

logger = logging.getLogger(__name__)

router = Router(name="attendance")

STALE_TEXT = "Список устарел. Откройте актуальный."


async def _answer(callback: CallbackQuery, text: str | None = None) -> None:
    # Без текста — просто гасим «часики»; с текстом — всплывающее окно
    await safe_answer(callback, text, show_alert=bool(text))


async def _edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=None)
    except TelegramAPIError as exc:
        logger.warning("не удалось изменить список явки: %s", exc.message)


async def _show(message: Message, markup: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_reply_markup(reply_markup=markup)
    except TelegramAPIError as exc:
        # «message is not modified» при повторном нажатии — не ошибка
        logger.debug("разметка списка явки не обновлена: %s", exc.message)


@router.callback_query(AttendanceCallback.filter())
async def on_attendance_callback(
    callback: CallbackQuery,
    callback_data: AttendanceCallback,
    attendance_ui: AttendanceUI,
    services: Services,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    admin_id = callback.from_user.id
    message = callback.message
    try:
        if not await check_admin(sessions, admin_id):
            await _answer(callback, NO_ACCESS)
            return
        if not isinstance(message, Message) or message.reply_markup is None:
            await _answer(callback, STALE_TEXT)
            return
        async with attendance_ui.lock(message.chat.id, message.message_id):
            await _dispatch(callback, message, callback_data, attendance_ui, services)
    except DomainError as exc:
        await _answer(callback, describe_error(exc))
    except TelegramAPIError as exc:
        logger.warning("ошибка Telegram в списке явки: %s", exc.message)
        await _answer(callback)
    except Exception as exc:
        logger.exception("ошибка при обработке кнопки явки")
        await _answer(callback, describe_error(exc))


async def _dispatch(
    callback: CallbackQuery,
    message: Message,
    data: AttendanceCallback,
    ui: AttendanceUI,
    services: Services,
) -> None:
    markup = message.reply_markup
    if markup is None:
        await _answer(callback, STALE_TEXT)
        return
    admin_id = callback.from_user.id
    chat_id, message_id = message.chat.id, message.message_id
    target = data.target
    if target == TARGET_NOOP:
        await _answer(callback)
    elif target == TARGET_DONE:
        await _on_done(callback, message, markup, data.event_id, ui, services)
    elif target.startswith(PAGE_PREFIX) and target[len(PAGE_PREFIX) :].isdigit():
        page = int(target[len(PAGE_PREFIX) :])
        new = await ui.page(admin_id, chat_id, message_id, markup, data.event_id, page)
        await _show(message, new)
        await _answer(callback)
    elif target.isdigit() and has_done(markup):
        # Обычный режим: в БД ничего не пишем, копим неявки в черновике
        new = await ui.toggle(admin_id, chat_id, message_id, markup, data.event_id, int(target))
        await _show(message, new)
        await _answer(callback)
    elif target.isdigit():
        await _show(message, await ui.correct(admin_id, markup, data.event_id, int(target)))
        await _answer(callback)
    else:
        await _answer(callback, STALE_TEXT)


async def _on_done(
    callback: CallbackQuery,
    message: Message,
    markup: InlineKeyboardMarkup,
    event_id: int,
    attendance_ui: AttendanceUI,
    services: Services,
) -> None:
    no_shows = attendance_ui.draft(message.chat.id, message.message_id, markup)
    try:
        result = await services.attendance.mark(callback.from_user.id, event_id, set(no_shows))
    except AlreadyMarked as exc:
        # Имя сырое: экранирование делает render_text_now
        text = services.settings.render_text_now(
            "text.attendance_marked_by", name=exc.marked_by_name or ANOTHER_ADMIN
        )
        attendance_ui.forget(message.chat.id, message.message_id)
        await _edit(message, text)
        await _answer(callback)
        return
    attendance_ui.forget(message.chat.id, message.message_id)
    lines = [f"Явка отмечена ✅. Неявок: {len(result.no_shows)}"]
    if result.failed_ban_user_ids:
        # Отметка сохранена; пересчёт банов дожмёт планировщик (задача ban_recheck)
        lines.append(
            f"⚠ Пересчёт банов отложен для участников: {len(result.failed_ban_user_ids)}. "
            "Бот повторит его автоматически."
        )
    if result.new_bans:
        lines.append(f"Выдано автобанов: {len(result.new_bans)}")
    await _edit(message, "\n".join(lines))
    await _answer(callback)
    await attendance_ui.close_others(event_id, message.chat.id, callback.from_user.full_name)
