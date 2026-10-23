"""Кнопки списка явки: переключатели ✅/🚫, «Готово» и исправление отметок."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.container import Services
from meetup_bot.db.models import Attendance
from meetup_bot.dialogs._common import NO_ACCESS, describe_error
from meetup_bot.services.attendance import AlreadyMarked
from meetup_bot.services.errors import DomainError
from meetup_bot.telegram.attendance_callbacks import TARGET_DONE, AttendanceCallback
from meetup_bot.telegram.attendance_ui import MARK_ATTENDED, MARK_NO_SHOW, AttendanceUI
from meetup_bot.telegram.common import check_admin, fit_alert

logger = logging.getLogger(__name__)

router = Router(name="attendance")

STALE_TEXT = "Список устарел. Откройте актуальный."


def _parse(button: InlineKeyboardButton) -> AttendanceCallback | None:
    if not button.callback_data or not button.callback_data.startswith("a:"):
        return None
    try:
        return AttendanceCallback.unpack(button.callback_data)
    except ValueError, TypeError:
        return None


def _is_no_show(button: InlineKeyboardButton) -> bool:
    return button.text.startswith(MARK_NO_SHOW)


def _toggled(markup: InlineKeyboardMarkup, target: str) -> InlineKeyboardMarkup:
    """Копия клавиатуры, где у кнопки `target` меняется префикс ✅ <-> 🚫."""
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            data = _parse(button)
            if data is not None and data.target == target:
                body = button.text.split(" ", 1)[1] if " " in button.text else button.text
                mark = MARK_ATTENDED if _is_no_show(button) else MARK_NO_SHOW
                button = button.model_copy(update={"text": f"{mark} {body}"})
            new_row.append(button)
        keyboard.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _no_show_ids(markup: InlineKeyboardMarkup) -> set[int]:
    result: set[int] = set()
    for row in markup.inline_keyboard:
        for button in row:
            data = _parse(button)
            if data is not None and data.target != TARGET_DONE and _is_no_show(button):
                result.add(int(data.target))
    return result


def _has_done(markup: InlineKeyboardMarkup) -> bool:
    for row in markup.inline_keyboard:
        for button in row:
            data = _parse(button)
            if data is not None and data.target == TARGET_DONE:
                return True
    return False


async def _answer(callback: CallbackQuery, text: str | None = None) -> None:
    try:
        await callback.answer(text=fit_alert(text) if text else None, show_alert=bool(text))
    except TelegramAPIError, ValueError:
        # ValueError — симулированный callback (ответ на текстовое сообщение): ответить нельзя
        logger.warning("не удалось ответить на callback явки", exc_info=True)


async def _edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=None)
    except TelegramAPIError as exc:
        logger.warning("не удалось изменить список явки: %s", exc.message)


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
        markup = message.reply_markup
        if callback_data.target == TARGET_DONE:
            event_id = callback_data.event_id
            await _on_done(callback, message, markup, event_id, attendance_ui, services)
        elif _has_done(markup):
            # Обычный режим: в БД ничего не пишем, меняется только разметка
            await message.edit_reply_markup(reply_markup=_toggled(markup, callback_data.target))
            await _answer(callback)
        else:
            await _on_correct(callback, message, markup, callback_data, services)
    except DomainError as exc:
        await _answer(callback, describe_error(exc))
    except TelegramAPIError as exc:
        # Например, «message is not modified» при двойном нажатии
        logger.warning("ошибка Telegram в списке явки: %s", exc.message)
        await _answer(callback)
    except Exception as exc:
        logger.exception("ошибка при обработке кнопки явки")
        await _answer(callback, describe_error(exc))


async def _on_correct(
    callback: CallbackQuery,
    message: Message,
    markup: InlineKeyboardMarkup,
    data: AttendanceCallback,
    services: Services,
) -> None:
    registration_id = int(data.target)
    # Новое состояние обратно показанному; разметку меняем только после записи в БД
    new = Attendance.ATTENDED if registration_id in _no_show_ids(markup) else Attendance.NO_SHOW
    await services.attendance.correct(callback.from_user.id, data.event_id, registration_id, new)
    await message.edit_reply_markup(reply_markup=_toggled(markup, data.target))
    await _answer(callback)


async def _on_done(
    callback: CallbackQuery,
    message: Message,
    markup: InlineKeyboardMarkup,
    event_id: int,
    attendance_ui: AttendanceUI,
    services: Services,
) -> None:
    try:
        result = await services.attendance.mark(
            callback.from_user.id, event_id, _no_show_ids(markup)
        )
    except AlreadyMarked as exc:
        # Имя сырое: экранирование делает render_text_now
        text = services.settings.render_text_now(
            "text.attendance_marked_by", name=exc.marked_by_name or "другой админ"
        )
        await _edit(message, text)
        await _answer(callback)
        return
    new_bans = len(result.new_bans)
    lines = [f"Явка отмечена ✔. Неявок: {len(result.no_shows)}"]
    if result.failed_ban_user_ids:
        # Отметка уже сохранена: дожимаем сбойные пересчёты и честно предупреждаем
        try:
            new_bans += len(await services.attendance.reevaluate_bans(event_id))
        except Exception:
            logger.exception("повторный пересчёт банов не удался (event_id=%s)", event_id)
        lines.append(
            f"⚠ Не удалось пересчитать баны для {len(result.failed_ban_user_ids)} "
            "участн.: проверьте список банов вручную."
        )
    if new_bans:
        lines.append(f"Выдано автобанов: {new_bans}")
    await _edit(message, "\n".join(lines))
    await _answer(callback)
    await attendance_ui.close_others(event_id, message.chat.id, callback.from_user.full_name)
