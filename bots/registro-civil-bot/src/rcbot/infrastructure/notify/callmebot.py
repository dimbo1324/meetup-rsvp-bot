"""Канал CallMeBot (SPEC 2.4, D8): голосовой звонок в Telegram, робот читает текст.

Если звонок с русским голосом не проходит, один раз пробуем английский голос и английский
текст (D8). Точные ответы сервиса проверяются командой `rcbot simulate` на этапе 3.
Английский текст говорит о появившихся местах, поэтому тестовой тревоге запасного звонка нет:
владелец не должен услышать «места появились», когда их нет.
"""

from __future__ import annotations

import logging

from rcbot.application.messages import CALL_MAX_CHARS, CALL_TEXT_EN
from rcbot.config.settings import CALLMEBOT_HOST
from rcbot.domain.models import Alert, AlertKind, Channel, DeliveryResult
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.notify._result import error_of, measured

logger = logging.getLogger(__name__)

API_URL = f"https://{CALLMEBOT_HOST}/start.php"
WARMUP_URL = f"https://{CALLMEBOT_HOST}/"
FALLBACK_LANG = "en-US-Standard-B"
# Тайм-аут одного запроса: русский звонок и запасной английский подряд (2 × 5 с) укладываются
# в общий лимит отправки оркестратора (12 с), и отказ успевает записаться как ошибка канала
CALLMEBOT_TIMEOUT_SEC = 5
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
        async with self._http.request("GET", API_URL, params=params) as response:
            body = (await response.text(errors="replace")).lower()
            if response.status != 200:
                return f"HTTP {response.status}"
            if any(marker in body for marker in _ERROR_MARKERS):
                return "сервис ответил ошибкой"
        return ""

    async def _attempt(self, text: str, lang: str) -> str:
        """Одна попытка звонка; сетевой сбой — текст ошибки, чтобы успеть запасной звонок."""
        return await error_of(lambda: self._call(text, lang))

    async def _call_with_fallback(self, alert: Alert) -> str:
        text = alert.call_text or alert.push_text or "Внимание, есть новости от бота."
        error = await self._attempt(text, self._lang)
        if error and self._lang != FALLBACK_LANG and alert.kind is not AlertKind.TEST:
            logger.warning(
                "CallMeBot: звонок (%s) не прошёл: %s; пробую английский", self._lang, error
            )
            error = await self._attempt(CALL_TEXT_EN, FALLBACK_LANG)
        return error

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult:
        result = await measured(
            Channel.CALL, alert, attempt, lambda: self._call_with_fallback(alert)
        )
        if result.error:
            logger.warning("CallMeBot: звонок не прошёл: %s", result.error)
        return result

    async def warmup(self) -> None:
        async with self._http.request("HEAD", WARMUP_URL) as response:
            await response.read()

    async def close(self) -> None:
        await self._http.close()
