"""Списки команд в меню Telegram: участникам — базовые, админам — ещё и меню управления."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

logger = logging.getLogger(__name__)

USER_COMMANDS = (
    BotCommand(command="start", description="Начать"),
    BotCommand(command="help", description="Как пользоваться"),
)
ADMIN_COMMANDS = (
    *USER_COMMANDS,
    BotCommand(command="menu", description="Меню администратора"),
    BotCommand(command="cancel", description="Выйти из текущего диалога"),
)


async def set_admin_commands(bot: Bot, user_id: int, *, is_admin: bool) -> None:
    """Включает или убирает админские команды в личке конкретного человека.

    Если человек ещё не писал боту, Telegram отвечает «chat not found»: это не ошибка —
    команды появятся, когда он нажмёт /start.
    """
    scope = BotCommandScopeChat(chat_id=user_id)
    try:
        if is_admin:
            await bot.set_my_commands(list(ADMIN_COMMANDS), scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except TelegramAPIError as exc:
        logger.info("команды для %s не обновлены: %s", user_id, exc.message)


async def sync_commands(bot: Bot, admin_ids: Iterable[int]) -> None:
    """При запуске: базовые команды всем в личке, расширенные — каждому админу."""
    try:
        await bot.set_my_commands(list(USER_COMMANDS), scope=BotCommandScopeAllPrivateChats())
    except TelegramAPIError:
        logger.exception("не удалось установить базовый список команд")
    for admin_id in admin_ids:
        await set_admin_commands(bot, admin_id, is_admin=True)
