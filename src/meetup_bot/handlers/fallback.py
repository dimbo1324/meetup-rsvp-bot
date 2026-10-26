"""Последний обработчик в личке: сообщения, которые не поняли ни команды, ни диалоги."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from meetup_bot.container import Services
from meetup_bot.user_info import UserInfo

UNKNOWN_ADMIN_TEXT = "Не понял. Меню — /menu, помощь — /help"
UNKNOWN_USER_TEXT = "Не понял. Помощь — /help"


async def on_unknown_message(message: Message, user_info: UserInfo, services: Services) -> None:
    is_admin = await services.users.is_admin(user_info.id)
    await message.answer(UNKNOWN_ADMIN_TEXT if is_admin else UNKNOWN_USER_TEXT)


def build_fallback_router() -> Router:
    """Новый роутер на каждый вызов: aiogram подключает роутер к родителю только один раз."""
    router = Router(name="fallback")
    router.message.register(on_unknown_message, F.chat.type == "private")
    return router
