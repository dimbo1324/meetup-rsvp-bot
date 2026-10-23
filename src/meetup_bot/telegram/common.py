"""Общие помощники обработчиков: форматирование дат, проверка прав, укорочение ответов."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.renderer.post import MONTHS, WEEKDAYS
from meetup_bot.repositories import admins

# Лимит Telegram на текст всплывающего окна answerCallbackQuery
ALERT_LIMIT = 200

GENERIC_ERROR_TEXT = "Что-то пошло не так, попробуйте ещё раз"


def date_parts(moment: datetime, tz: ZoneInfo) -> tuple[str, str, str]:
    """(«суббота», «1 ноября», «17:00») в часовом поясе клуба."""
    local = moment.astimezone(tz)
    return WEEKDAYS[local.weekday()], f"{local.day} {MONTHS[local.month - 1]}", f"{local:%H:%M}"


def format_moment(moment: datetime, tz: ZoneInfo) -> str:
    """«суббота, 1 ноября, 17:00»."""
    weekday, date, time_ = date_parts(moment, tz)
    return f"{weekday}, {date}, {time_}"


def fit_alert(text: str) -> str:
    """Режет текст до лимита всплывающего окна, чтобы Telegram не отклонил ответ целиком."""
    if len(text) <= ALERT_LIMIT:
        return text
    return text[: ALERT_LIMIT - 1].rstrip() + "…"


async def check_admin(sessions: async_sessionmaker[AsyncSession], user_id: int) -> bool:
    """Права проверяются в самом обработчике, на каждом админском действии (раздел 3)."""
    async with sessions() as session:
        return await admins.is_admin(session, user_id)
