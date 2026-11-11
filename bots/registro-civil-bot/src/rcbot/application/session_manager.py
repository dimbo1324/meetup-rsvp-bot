"""Сессия сайта (SPEC 2.3): cookies только в памяти, вход под блокировкой, лимит входов в час,
пауза после тревоги (D5), учёт времени жизни сессии (D2)."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Sequence

from rcbot.application.ports import Authenticator, Clock, SessionCookie
from rcbot.domain.errors import ReloginBudgetExceeded, ReloginDeferred

logger = logging.getLogger(__name__)

HOUR = 3600.0
QUIET_AFTER_ALERT_SEC = 600.0

CookieSink = Callable[[Sequence[SessionCookie]], None]


class SessionManager:
    def __init__(
        self,
        authenticator: Authenticator,
        clock: Clock,
        max_per_hour: int,
        quiet_after_alert_sec: float = QUIET_AFTER_ALERT_SEC,
    ) -> None:
        self._auth = authenticator
        self._clock = clock
        self._max_per_hour = max_per_hour
        self._quiet_after_alert = quiet_after_alert_sec
        self._lock = asyncio.Lock()
        self._attempts: deque[float] = deque()
        self._sinks: list[CookieSink] = []
        self._cookies: list[SessionCookie] = []
        self._logged_in_at: float | None = None
        self._quiet_until = 0.0
        self.logins = 0
        self.session_lifetimes: list[float] = []

    def subscribe(self, sink: CookieSink) -> None:
        self._sinks.append(sink)
        if self._cookies:
            sink(self._cookies)

    @property
    def has_session(self) -> bool:
        return bool(self._cookies)

    def quiet_after_alert(self) -> None:
        """Владелец сейчас записывается сам: его вход может выбить сессию бота, а наш — его."""
        self._quiet_until = self._clock.monotonic() + self._quiet_after_alert

    def quiet_remaining(self) -> float:
        return max(0.0, self._quiet_until - self._clock.monotonic())

    def logins_last_hour(self) -> int:
        self._prune(self._clock.monotonic())
        return len(self._attempts)

    def _prune(self, now: float) -> None:
        while self._attempts and now - self._attempts[0] >= HOUR:
            self._attempts.popleft()

    async def ensure(self) -> None:
        if not self._cookies:
            await self.relogin("нет сессии")

    def mark_expired(self) -> None:
        if self._logged_in_at is not None:
            lifetime = self._clock.monotonic() - self._logged_in_at
            self.session_lifetimes.append(lifetime)
            logger.info("сессия истекла через %.0f мин", lifetime / 60)
        self._cookies = []
        self._logged_in_at = None

    async def relogin(self, reason: str, *, force: bool = False) -> None:
        async with self._lock:
            now = self._clock.monotonic()
            if self._cookies and self._logged_in_at is not None and not force:
                # Другой вызов уже вошёл, пока мы ждали блокировку
                return
            if (remaining := self.quiet_remaining()) > 0:
                raise ReloginDeferred(f"вход отложен на {remaining:.0f} с после тревоги")
            self._prune(now)
            if len(self._attempts) >= self._max_per_hour:
                raise ReloginBudgetExceeded(
                    f"уже {len(self._attempts)} входов за час (лимит {self._max_per_hour})"
                )
            self._attempts.append(now)
            logger.info("вход на сайт: %s", reason)
            cookies = await self._auth.login()
            self._cookies = list(cookies)
            self._logged_in_at = self._clock.monotonic()
            self.logins += 1
            for sink in self._sinks:
                sink(self._cookies)
