"""Безопасные обёртки над портами: сбой Telegram после коммита не должен терять результат.

Состояние к моменту уведомления уже сохранено, поэтому ошибка доставки только логируется
(без текста сообщения и персональных данных) и не пробрасывается вызывающему.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from meetup_bot.services.ports import Notifier, PostRefresher

logger = logging.getLogger(__name__)

# Сколько при остановке ждём недоставленные уведомления, прежде чем их отменить
BACKGROUND_CLOSE_TIMEOUT = 10.0


async def safe_send_dm(notifier: Notifier, user_id: int, text: str) -> bool:
    try:
        return await notifier.send_dm(user_id, text)
    except Exception:
        logger.exception("не удалось отправить личное сообщение (user_id=%s)", user_id)
        return False


async def safe_send_service(notifier: Notifier, text: str) -> bool:
    try:
        return await notifier.send_service(text)
    except Exception:
        logger.exception("не удалось отправить сообщение в служебный чат")
        return False


def safe_schedule(post_refresher: PostRefresher, event_id: int) -> None:
    try:
        post_refresher.schedule(event_id)
    except Exception:
        logger.exception("не удалось поставить пересборку постов (event_id=%s)", event_id)


class BackgroundTasks:
    """Реестр фоновых побочных эффектов (уведомления после коммита).

    Ответ на нажатие кнопки не должен ждать Telegram: при лимите запросов отправка
    уведомления может занять десятки секунд, а пользователь всё это время видит «часики».
    Ссылки на задачи хранятся до завершения: цикл событий держит их слабо, и без этого
    сборщик мусора мог бы убить задачу на полуслове.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, effect: Coroutine[Any, Any, object]) -> None:
        task = asyncio.create_task(self._guarded(effect))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _guarded(effect: Coroutine[Any, Any, object]) -> None:
        try:
            await effect
        except Exception:
            logger.exception("фоновое уведомление завершилось ошибкой")

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def drain(self) -> None:
        """Ждёт все запущенные задачи, включая порождённые по ходу ожидания."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        """Останов приложения: даёт задачам доработать, затем отменяет оставшиеся."""
        try:
            async with asyncio.timeout(BACKGROUND_CLOSE_TIMEOUT):
                await self.drain()
        except TimeoutError:
            # Отмена `drain` через gather уже отменила и сами задачи
            logger.warning(
                "фоновые уведомления не завершились за %.0f с, отменены", BACKGROUND_CLOSE_TIMEOUT
            )
