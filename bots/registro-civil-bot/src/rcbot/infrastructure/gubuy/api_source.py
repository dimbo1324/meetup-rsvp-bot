"""`ApiSource` — основной источник (SPEC 2.3): прямой запрос к API календаря через aiohttp.

Ответ проверяется моделью; неожиданный формат → `UnexpectedResponse` и карантин. Признак
истёкшей сессии — переадресация на вход или HTML вместо ответа API. Разбор ответа пишется по
дампу разведки (этап 3), до этого источник не включается.
"""

from __future__ import annotations

import json
import time as monotonic_time
from collections.abc import Sequence
from http.cookies import SimpleCookie
from pathlib import Path

import aiohttp
from yarl import URL

from rcbot.application.ports import Clock, SessionCookie
from rcbot.domain.errors import (
    RateLimited,
    SessionExpired,
    SiteUnavailable,
    SourceNotConfigured,
    UnexpectedResponse,
)
from rcbot.domain.models import AvailabilitySnapshot, SourceKind, WatchTarget
from rcbot.infrastructure.gubuy.pages import (
    PAGE_ERRORS,
    RATE_LIMIT_STATUSES,
    PageKind,
    classify,
    is_login_redirect,
)
from rcbot.infrastructure.gubuy.quarantine import quarantine
from rcbot.infrastructure.gubuy.schemas import parse_disponibilidades
from rcbot.infrastructure.http.client import SafeHttp

# Прежнее имя функции: переадресация на вход распознаётся в pages.py
_is_login_redirect = is_login_redirect

# Чем источник объясняет страницу, пришедшую вместо ответа API
_PAGE_TEXTS = {
    PageKind.LOGIN: "вместо ответа API пришла страница входа",
    PageKind.STEP1: "сайт требует пройти шаг 1",
    PageKind.BLOCKED: "страница блокировки вместо ответа API",
}


class ApiSource:
    def __init__(
        self, http: SafeHttp, api_url: str, agenda_url: str, quarantine_dir: Path, clock: Clock
    ) -> None:
        self._http = http
        self._api_url = api_url
        self._agenda_url = agenda_url
        self._quarantine_dir = quarantine_dir
        self._clock = clock

    @property
    def kind(self) -> SourceKind:
        return SourceKind.API

    @property
    def configured(self) -> bool:
        return bool(self._api_url)

    def use_cookies(self, cookies: Sequence[SessionCookie]) -> None:
        jar = self._http.cookie_jar
        jar.clear()
        for cookie in cookies:
            host = cookie.domain.lstrip(".")
            morsels: SimpleCookie = SimpleCookie()
            morsels[cookie.name] = cookie.value
            morsels[cookie.name]["path"] = cookie.path
            if cookie.domain.startswith("."):
                # Cookie с точкой действует и на поддомены: без атрибута domain jar сделал бы её
                # «только для этого хоста», и родительский домен (.mec.gub.uy) не дошёл бы до bpmgob
                morsels[cookie.name]["domain"] = host
            jar.update_cookies(morsels, response_url=URL(f"https://{host}/"))

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        if not self._api_url:
            raise SourceNotConfigured("адрес API не задан (RCBOT_API_URL) — нужен дамп разведки")
        started = monotonic_time.perf_counter()
        headers = {"X-Requested-With": "XMLHttpRequest", "Referer": self._agenda_url}
        try:
            async with self._http.request("GET", self._api_url, headers=headers) as response:
                status = response.status
                location = response.headers.get("Location", "")
                content_type = response.headers.get("Content-Type", "")
                body = await response.text(errors="replace")
        except (aiohttp.ClientError, TimeoutError) as error:
            raise SiteUnavailable(f"API недоступен: {type(error).__name__}") from None
        latency_ms = (monotonic_time.perf_counter() - started) * 1000

        if 300 <= status < 400:
            if is_login_redirect(location):
                raise SessionExpired("API переадресует на вход")
            raise self._unexpected(status, content_type, body, "неожиданная переадресация")
        if status in RATE_LIMIT_STATUSES:
            raise RateLimited(f"API ответил {status}")
        if status >= 500:
            raise SiteUnavailable(f"API ответил {status}")
        if "json" not in content_type.lower():
            page = classify(self._api_url, body, status)
            if (error := PAGE_ERRORS.get(page)) is not None:
                raise error(_PAGE_TEXTS[page])
            raise self._unexpected(status, content_type, body, "HTML вместо ответа API")
        try:
            slots = parse_disponibilidades(json.loads(body))
        except (ValueError, UnexpectedResponse) as error:
            raise self._unexpected(status, content_type, body, str(error)) from None
        return AvailabilitySnapshot(target.id, self._clock.now(), slots, SourceKind.API, latency_ms)

    def _unexpected(
        self, status: int, content_type: str, body: str, why: str
    ) -> UnexpectedResponse:
        path = quarantine(
            self._quarantine_dir, self._clock.now(), "api", status, content_type, body
        )
        return UnexpectedResponse(f"{why}; снимок в карантине: {path.name}")

    async def close(self) -> None:
        await self._http.close()
