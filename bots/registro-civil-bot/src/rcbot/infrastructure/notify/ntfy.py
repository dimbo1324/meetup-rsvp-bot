"""Канал ntfy (SPEC 2.4): push с максимальным приоритетом. Тема — секрет; в тексте только даты."""

from __future__ import annotations

import logging

from rcbot.domain.models import Alert, Channel, DeliveryResult
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.notify._result import measured

logger = logging.getLogger(__name__)

# ntfy — сторонний сервис (SPEC 2.4): без `push_text` полный `alert.text` с номером заявки и
# ссылкой туда не уходит, только общая фраза. Поля `click` нет вовсе (решение R10): ссылка на
# шаг «Agenda» содержит номер этапа и остаётся только в Telegram
GENERIC_PUSH_TEXT = "Есть новости от бота. Подробности — в Telegram."
# Тайм-аут HTTP-клиента канала: меньше общего лимита отправки оркестратора (12 с)
NTFY_TIMEOUT_SEC = 10


class NtfyNotifier:
    def __init__(self, http: SafeHttp, server: str, topic: str) -> None:
        self._http = http
        self._server = server.rstrip("/")
        self._topic = topic

    @property
    def channel(self) -> Channel:
        return Channel.NTFY

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        payload: dict[str, object] = {
            "topic": self._topic,
            "title": alert.push_title or "rcbot",
            "message": alert.push_text or GENERIC_PUSH_TEXT,
            "priority": 5,
            "tags": ["rotating_light"],
        }

        async def post() -> str:
            async with self._http.request("POST", f"{self._server}/", json=payload) as response:
                return "" if response.status == 200 else f"HTTP {response.status}"

        result = await measured(Channel.NTFY, alert, attempt, post)
        if result.error:
            logger.warning("ntfy: отправка не удалась: %s", result.error)
        return result

    async def warmup(self) -> None:
        async with self._http.request("GET", f"{self._server}/v1/health") as response:
            await response.read()

    async def close(self) -> None:
        await self._http.close()
