"""Пинг внешнего «сторожа» (SPEC 6.1, например healthchecks.io): пустой запрос без данных."""

from __future__ import annotations

import aiohttp

from rcbot.infrastructure.http.client import SafeHttp


class HttpWatchdog:
    def __init__(self, http: SafeHttp, url: str) -> None:
        self._http = http
        self._url = url

    async def ping(self) -> bool:
        try:
            async with self._http.request("GET", self._url) as response:
                status: int = response.status
                return status < 400
        except aiohttp.ClientError, TimeoutError:
            return False

    async def close(self) -> None:
        await self._http.close()
