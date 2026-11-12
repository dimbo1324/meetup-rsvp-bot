"""Telegram-интерфейс: Dispatcher, фильтр владельца, команды, меню, long polling (SPEC 2.5)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import ExceptionTypeFilter
from aiogram.types import BotCommand, ErrorEvent
from aiogram_dialog import setup_dialogs
from aiogram_dialog.api.exceptions import UnknownIntent, UnknownState

from rcbot.application.control import Controller
from rcbot.bot import texts
from rcbot.bot.dialogs.menu import build_menu_dialog
from rcbot.bot.handlers import ack, commands
from rcbot.bot.middlewares import OwnerOnlyMiddleware

ALLOWED_UPDATES = ["message", "callback_query"]

COMMANDS = [
    BotCommand(command="status", description="состояние"),
    BotCommand(command="pause", description="пауза [минуты]"),
    BotCommand(command="resume", description="продолжить"),
    BotCommand(command="test", description="проверить каналы"),
    BotCommand(command="last", description="последние инциденты"),
    BotCommand(command="interval", description="интервал опроса, с"),
    BotCommand(command="relogin", description="войти заново"),
    BotCommand(command="cleanup", description="очистить старые данные"),
    BotCommand(command="menu", description="меню"),
]


async def _on_stale_dialog(event: ErrorEvent) -> None:
    """Диалоги живут в памяти: после перезапуска кнопки старого меню неизвестны, а не «зависают»."""
    callback = event.update.callback_query
    if callback is None:
        return
    try:
        await callback.answer(texts.MENU_STALE)
    except TelegramAPIError:  # нажатие слишком старое для ответа — отвечать уже некуда
        return


def build_dispatcher(controller: Controller, tz: ZoneInfo, owner_chat_id: int) -> Dispatcher:
    dispatcher = Dispatcher(controller=controller, tz=tz)
    owner_only = OwnerOnlyMiddleware(owner_chat_id)
    # Фильтр стоит снаружи: чужое обновление не доходит ни до команд, ни до меню
    dispatcher.message.outer_middleware(owner_only)
    dispatcher.callback_query.outer_middleware(owner_only)
    dispatcher.include_router(ack.build_router())
    dispatcher.include_router(commands.build_router())
    dispatcher.include_router(build_menu_dialog())
    dispatcher.errors.register(_on_stale_dialog, ExceptionTypeFilter(UnknownIntent, UnknownState))
    setup_dialogs(dispatcher)
    return dispatcher


async def run_polling(bot: Bot, dispatcher: Dispatcher) -> None:
    await bot.set_my_commands(COMMANDS)
    await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES, handle_signals=False)
