"""Общие помощники обработчиков: проверка прав, укорочение и отправка ответов, листание."""

from __future__ import annotations

import logging

from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.repositories import admins
from meetup_bot.texts.render import clip, telegram_length

__all__ = [
    "ALERT_LIMIT",
    "GENERIC_ERROR_TEXT",
    "MAX_RETRY_AFTER",
    "START_ABOUT",
    "START_REMIND",
    "check_admin",
    "fit_alert",
    "page_count",
    "safe_answer",
    "telegram_length",
]

logger = logging.getLogger(__name__)

# Лимит Telegram на текст всплывающего окна answerCallbackQuery
ALERT_LIMIT = 200

GENERIC_ERROR_TEXT = "Что-то пошло не так, попробуйте ещё раз."

# Верхняя граница ожидания при лимитах (RetryAfter) и сбоях сети, чтобы отправка не зависала
MAX_RETRY_AFTER = 30.0

# Параметры deep-link `t.me/<бот>?start=…`
START_REMIND = "remind"
START_ABOUT = "about"


def fit_alert(text: str) -> str:
    """Режет текст до лимита всплывающего окна, чтобы Telegram не отклонил ответ целиком."""
    return clip(text, ALERT_LIMIT)


async def safe_answer(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = True,
    url: str | None = None,
) -> None:
    """Отвечает на callback, не роняя обработчик.

    Ответ — последний шаг: если он не удался («query is too old», сеть, симулированный
    callback), пользователь уже увидел результат на посте или в меню, повторять нечего.
    """
    try:
        await callback.answer(
            text=fit_alert(text) if text else None, show_alert=show_alert, url=url
        )
    except Exception:
        logger.warning("не удалось ответить на callback", exc_info=True)


def page_count(total: int, size: int) -> int:
    """Число страниц по `size` элементов; пустой список — одна (пустая) страница."""
    return max(1, -(-total // size))


async def check_admin(sessions: async_sessionmaker[AsyncSession], user_id: int) -> bool:
    """Права проверяются в самом обработчике, на каждом админском действии (раздел 3)."""
    async with sessions() as session:
        return await admins.is_admin(session, user_id)
