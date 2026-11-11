"""Канал Telegram (SPEC 2.4): сообщение от собственного бота владельца, кнопки «Вижу» и «Готово».

Бот aiogram общий с Telegram-интерфейсом: одно прогретое соединение с api.telegram.org.
"""

from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from rcbot.domain.models import Alert, AlertAction, Channel, DeliveryResult

logger = logging.getLogger(__name__)

CALLBACK_ACK = "ack"
CALLBACK_MANUAL_DONE = "manual_done"
REQUEST_TIMEOUT_SEC = 10
MAX_TEXT = 4096

_BUTTONS = {
    AlertAction.ACK: ("✅ Вижу", CALLBACK_ACK),
    AlertAction.MANUAL_DONE: ("✅ Готово, шаг 1 пройден", CALLBACK_MANUAL_DONE),
}


def keyboard(action: AlertAction) -> InlineKeyboardMarkup | None:
    if action not in _BUTTONS:
        return None
    text, data = _BUTTONS[action]
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data)]]
    )


class TelegramNotifier:
    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    @property
    def channel(self) -> Channel:
        return Channel.TELEGRAM

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        started = time.perf_counter()
        prefix = f"🔁 Повтор {attempt}\n" if attempt > 1 else ""
        try:
            await self._bot.send_message(
                self._chat_id,
                (prefix + alert.text)[:MAX_TEXT],
                reply_markup=keyboard(alert.action),
                disable_web_page_preview=True,
                request_timeout=REQUEST_TIMEOUT_SEC,
            )
        except TelegramRetryAfter as error:
            return self._failed(
                started, attempt, alert, f"Telegram просит подождать {error.retry_after} с"
            )
        except (TelegramAPIError, OSError, TimeoutError) as error:
            return self._failed(started, attempt, alert, type(error).__name__)
        return DeliveryResult(
            Channel.TELEGRAM,
            True,
            (time.perf_counter() - started) * 1000,
            attempt,
            "",
            alert.incident_id,
        )

    @staticmethod
    def _failed(started: float, attempt: int, alert: Alert, error: str) -> DeliveryResult:
        logger.warning("Telegram: отправка не удалась: %s", error)
        return DeliveryResult(
            Channel.TELEGRAM,
            False,
            (time.perf_counter() - started) * 1000,
            attempt,
            error,
            alert.incident_id,
        )

    async def warmup(self) -> None:
        await self._bot.get_me(request_timeout=REQUEST_TIMEOUT_SEC)

    async def close(self) -> None:
        """Сессию бота закрывает владелец объекта Bot (приложение)."""
