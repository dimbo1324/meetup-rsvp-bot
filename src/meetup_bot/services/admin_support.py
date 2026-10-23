"""Общие мелочи админских сервисов: шаблоны текстов, отправка уведомлений после коммита."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from meetup_bot.services.ports import Notifier

logger = logging.getLogger(__name__)

# Совместимо с будущим сервисом текстов: (ключ, параметры) -> готовая строка
RenderText = Callable[[str, dict[str, Any]], str]

_DEFAULT_TEMPLATES = {
    "event_moved": "Встреча перенесена: новое время — {starts_at}.",
    "event_cancelled": "Встреча {starts_at} отменена.{reason_line}",
    "promoted": "Освободилось место — вы записаны на встречу {starts_at}.",
}


def default_render_text(key: str, params: dict[str, Any]) -> str:
    return _DEFAULT_TEMPLATES[key].format(**params)


def format_local(moment: datetime, timezone: str) -> str:
    return moment.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M")


async def send_dms(notifier: Notifier, user_ids: list[int], text: str) -> None:
    """Рассылка после коммита: сбой отправки не должен ломать уже сохранённую операцию."""
    for user_id in user_ids:
        try:
            await notifier.send_dm(user_id, text)
        except Exception:
            logger.exception("не удалось отправить личное сообщение пользователю %s", user_id)
