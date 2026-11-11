"""Проверка владельца в диалогах: права админа проверяет `_common.guard`."""

from __future__ import annotations

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager

from meetup_bot.dialogs._common import actor_id, get_services
from meetup_bot.telegram.common import safe_answer
from meetup_bot.telegram.errors_text import NO_ACCESS


async def is_owner(manager: DialogManager) -> bool:
    """Роль владельца перепроверяется на каждом действии, как и права админа."""
    return await get_services(manager).users.is_owner(actor_id(manager))


async def guard_owner(event: Message | CallbackQuery, manager: DialogManager) -> bool:
    """True, если нажавший — владелец; иначе отвечает «Нет доступа»."""
    if await is_owner(manager):
        return True
    if isinstance(event, CallbackQuery):
        await safe_answer(event, NO_ACCESS)
    else:
        await event.answer(NO_ACCESS)
    return False
