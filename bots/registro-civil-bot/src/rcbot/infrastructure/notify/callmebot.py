"""Канал CallMeBot (SPEC 2.4, D8): голосовой звонок в Telegram, робот читает текст.

Если звонок с русским голосом не проходит, один раз пробуем английский голос и английский
текст (D8). Точные ответы сервиса проверяются командой `rcbot simulate` на этапе 3.
"""

from __future__ import annotations

import logging
import time

import aiohttp

from rcbot.application.messages import CALL_MAX_CHARS, CALL_TEXT_EN
from rcbot.domain.models import Alert, Channel, DeliveryResult
from rcbot.infrastructure.http.client import SafeHttp

logger = logging.getLogger(__name__)

API_URL = "https://api.callmebot.com/start.php"
FALLBACK_LANG = "en-US-Standard-B"
_ERROR_MARKERS = ("error", "not authorized", "invalid", "you need to")


class CallMeBotNotifier:
    def __init__(self, http: SafeHttp, user: str, lang: str) -> None:
        self._http = http
        self._user = user if user.startswith("@") else f"@{user}"
        self._lang = lang

    @property
    def channel(self) -> Channel:
        return Channel.CALL

    async def _call(self, text: str, lang: str) -> str:
        params = {"user": self._user, "text": text[:CALL_MAX_CHARS], "lang": lang, "rpt": "2"}
        try:
            async with self._http.request("GET", API_URL, params=params) as response:
                body = (await response.text(errors="replace")).lower()
                if response.status != 200:
                    return f"HTTP {response.status}"
                if any(marker in body for marker in _ERROR_MARKERS):
                    return "сервис ответил ошибкой"
        except (aiohttp.ClientError, TimeoutError) as exc:
            return type(exc).__name__
        return ""

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        started = time.perf_counter()
        text = alert.call_text or alert.push_text or "Внимание, есть новости от бота."
        error = await self._call(text, self._lang)
        if error and self._lang != FALLBACK_LANG:
            logger.warning(
                "CallMeBot: звонок (%s) не прошёл: %s; пробую английский", self._lang, error
            )
            error = await self._call(CALL_TEXT_EN, FALLBACK_LANG)
        if error:
            logger.warning("CallMeBot: звонок не прошёл: %s", error)
        latency = (time.perf_counter() - started) * 1000
        return DeliveryResult(Channel.CALL, not error, latency, attempt, error, alert.incident_id)

    async def warmup(self) -> None:
        async with self._http.request("HEAD", "https://api.callmebot.com/") as response:
            await response.read()

    async def close(self) -> None:
        await self._http.close()
