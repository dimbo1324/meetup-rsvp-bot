"""Канал Telegram (SPEC 2.4): сообщение от собственного бота владельца, кнопки «Вижу» и «Готово».

Бот aiogram общий с Telegram-интерфейсом: одно прогретое соединение с api.telegram.org.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from rcbot.domain.models import Alert, AlertAction, Channel, DeliveryResult
from rcbot.infrastructure.notify._result import measured

logger = logging.getLogger(__name__)

# callback_data кнопок — значения действий домена: на них же настроены обработчики слоя bot
CALLBACK_ACK = AlertAction.ACK.value
CALLBACK_MANUAL_DONE = AlertAction.MANUAL_DONE.value
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
        prefix = f"🔁 Повтор {attempt}\n" if attempt > 1 else ""

        async def deliver() -> str:
            try:
                await self._bot.send_message(
                    self._chat_id,
                    (prefix + alert.text)[:MAX_TEXT],
                    reply_markup=keyboard(alert.action),
                    disable_web_page_preview=True,
                    request_timeout=REQUEST_TIMEOUT_SEC,
                )
            except TelegramRetryAfter as error:
                return f"Telegram просит подождать {error.retry_after} с"
            except (TelegramAPIError, OSError) as error:
                return type(error).__name__
            return ""

        result = await measured(Channel.TELEGRAM, alert, attempt, deliver)
        if result.error:
            logger.warning("Telegram: отправка не удалась: %s", result.error)
        return result

    async def warmup(self) -> None:
        await self._bot.get_me(request_timeout=REQUEST_TIMEOUT_SEC)

    async def close(self) -> None:
        """Сессию бота закрывает владелец объекта Bot (приложение)."""
