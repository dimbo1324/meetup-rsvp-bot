"""Канал ntfy (SPEC 2.4): push с максимальным приоритетом. Тема — секрет; в тексте только даты."""

from __future__ import annotations

import logging
import time

import aiohttp

from rcbot.domain.models import Alert, Channel, DeliveryResult
from rcbot.infrastructure.http.client import SafeHttp

logger = logging.getLogger(__name__)

# ntfy — сторонний сервис (SPEC 2.4): без `push_text` полный `alert.text` с номером заявки и
# ссылкой туда не уходит, только общая фраза
GENERIC_PUSH_TEXT = "Есть новости от бота. Подробности — в Telegram."


class NtfyNotifier:
    def __init__(self, http: SafeHttp, server: str, topic: str) -> None:
        self._http = http
        self._server = server.rstrip("/")
        self._topic = topic

    @property
    def channel(self) -> Channel:
        return Channel.NTFY

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        started = time.perf_counter()
        payload: dict[str, object] = {
            "topic": self._topic,
            "title": alert.push_title or "rcbot",
            "message": alert.push_text or GENERIC_PUSH_TEXT,
            "priority": 5,
            "tags": ["rotating_light"],
        }
        if alert.click_url:
            payload["click"] = alert.click_url
        error = ""
        try:
            async with self._http.request("POST", f"{self._server}/", json=payload) as response:
                if response.status != 200:
                    error = f"HTTP {response.status}"
        except (aiohttp.ClientError, TimeoutError) as exc:
            error = type(exc).__name__
        if error:
            logger.warning("ntfy: отправка не удалась: %s", error)
        latency = (time.perf_counter() - started) * 1000
        return DeliveryResult(Channel.NTFY, not error, latency, attempt, error, alert.incident_id)

    async def warmup(self) -> None:
        async with self._http.request("GET", f"{self._server}/v1/health") as response:
            await response.read()

    async def close(self) -> None:
        await self._http.close()
