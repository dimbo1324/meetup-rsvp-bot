"""Безопасные обёртки над портами: сбой Telegram после коммита не должен терять результат.

Состояние к моменту уведомления уже сохранено, поэтому ошибка доставки только логируется
(без текста сообщения и персональных данных) и не пробрасывается вызывающему.
"""

from __future__ import annotations

import logging

from meetup_bot.services.ports import Notifier, PostRefresher

logger = logging.getLogger(__name__)


async def safe_send_dm(notifier: Notifier, user_id: int, text: str) -> bool:
    try:
        return await notifier.send_dm(user_id, text)
    except Exception:
        logger.exception("не удалось отправить личное сообщение (user_id=%s)", user_id)
        return False


async def safe_send_service(notifier: Notifier, text: str) -> None:
    try:
        await notifier.send_service(text)
    except Exception:
        logger.exception("не удалось отправить сообщение в служебный чат")


def safe_schedule(post_refresher: PostRefresher, event_id: int) -> None:
    try:
        post_refresher.schedule(event_id)
    except Exception:
        logger.exception("не удалось поставить пересборку постов (event_id=%s)", event_id)
