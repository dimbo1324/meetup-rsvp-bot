"""Обработчики Telegram-слоя и сборка корневого роутера."""

from __future__ import annotations

from aiogram import Router

from meetup_bot.handlers import attendance, errors, inline, signup, start
from meetup_bot.handlers.fallback import build_fallback_router
from meetup_bot.middlewares.user import UserMiddleware

# Совпадает с разделом 8.7 спецификации; передаётся в `start_polling(allowed_updates=...)`
ALLOWED_UPDATES = [
    "message",
    "callback_query",
    "inline_query",
    "chosen_inline_result",
    "my_chat_member",
]


def build_router() -> Router:
    """Корневой роутер: ошибки, запись по кнопкам, inline, личка, диалоги, «не понял»."""
    root = Router(name="root")
    middleware = UserMiddleware()
    # Outer-middleware на корне срабатывает до фильтров вложенных роутеров
    for observer in (
        root.message,
        root.callback_query,
        root.inline_query,
        root.chosen_inline_result,
    ):
        observer.outer_middleware(middleware)
    from meetup_bot.dialogs import build_dialog_router

    # Команды /start, /help — раньше диалогов: иначе открытое окно с полем ввода приняло бы
    # их за ответ админа
    root.include_routers(
        errors.router,
        signup.router,
        attendance.router,
        inline.router,
        start.router,
        build_dialog_router(),
        # Строго последним: иначе перехватит ввод диалогов
        build_fallback_router(),
    )
    return root
