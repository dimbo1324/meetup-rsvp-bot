"""Общие помощники обработчиков: проверка прав, укорочение ответов."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.repositories import admins
from meetup_bot.texts.render import clip, telegram_length

__all__ = ["ALERT_LIMIT", "GENERIC_ERROR_TEXT", "check_admin", "fit_alert", "telegram_length"]

# Лимит Telegram на текст всплывающего окна answerCallbackQuery
ALERT_LIMIT = 200

GENERIC_ERROR_TEXT = "Что-то пошло не так, попробуйте ещё раз"


def fit_alert(text: str) -> str:
    """Режет текст до лимита всплывающего окна, чтобы Telegram не отклонил ответ целиком."""
    return clip(text, ALERT_LIMIT)


async def check_admin(sessions: async_sessionmaker[AsyncSession], user_id: int) -> bool:
    """Права проверяются в самом обработчике, на каждом админском действии (раздел 3)."""
    async with sessions() as session:
        return await admins.is_admin(session, user_id)
