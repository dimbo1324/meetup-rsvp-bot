"""Живость бота (решение R6): «жив / завис» видно в status.json журнала и в Telegram.

Раз в 30 секунд обновляется `status.json` с меткой `alive_at`. Цикл опроса считается зависшим,
если проверка идёт дольше `HUNG_CHECK_SEC` или следующая проверка опоздала больше чем на
`HUNG_LATE_SEC`. Тогда: запись в журнал, сообщение владельцу, перезапуск цикла опроса и
источника (браузер закрывается и создаётся заново). Если сам процесс встанет, `alive_at`
перестанет обновляться — это видно по файлу, а внешний «сторож» сообщит о простое.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime

from rcbot import __version__
from rcbot.application.poll_loop import FETCH_TIMEOUT_SEC, PollLoop
from rcbot.application.ports import Clock, Journal

logger = logging.getLogger(__name__)

STATUS_EVERY_SEC = 30.0
HUNG_CHECK_SEC = FETCH_TIMEOUT_SEC + 90.0
HUNG_LATE_SEC = 180.0
# Если после перезапуска цикл всё ещё завис, лечим снова не чаще, чем раз в это время
HUNG_RETRY_SEC = 300.0


class LivenessMonitor:
    def __init__(
        self,
        loop: PollLoop,
        journal: Journal,
        clock: Clock,
        notify: Callable[[str], Awaitable[None]],
        restart_poll: Callable[[], Awaitable[None]],
        extra: Callable[[], Awaitable[dict[str, object]]] | None = None,
    ) -> None:
        self._loop = loop
        self._journal = journal
        self._clock = clock
        self._notify = notify
        self._restart_poll = restart_poll
        self._extra = extra
        self.hung_since: datetime | None = None
        self.recoveries = 0
        self._last_recovery = 0.0

    def diagnose(self) -> str:
        """Пустая строка — цикл жив; иначе — почему он считается зависшим."""
        mono = self._clock.monotonic()
        started = self._loop.tick_started_at
        if started is not None and mono - started > HUNG_CHECK_SEC:
            return f"проверка идёт {mono - started:.0f} с (норма — до {FETCH_TIMEOUT_SEC:.0f} с)"
        late = mono - self._loop.next_due
        if started is None and self._loop.next_due > 0 and late > HUNG_LATE_SEC:
            return f"следующая проверка опаздывает на {late:.0f} с"
        return ""

    async def tick(self) -> None:
        problem = self.diagnose()
        now = self._clock.now()
        if problem and self.hung_since is None:
            self.hung_since = now
            await self._recover(problem)
        elif problem and self._clock.monotonic() - self._last_recovery >= HUNG_RETRY_SEC:
            # Перезапуск не помог (новая проверка снова повисла): без повтора бот молчал бы вечно
            await self._recover(problem)
        elif not problem and self.hung_since is not None:
            self._journal.event("liveness.recovered", "цикл опроса снова работает")
            self.hung_since = None
        snapshot: dict[str, object] = {
            "alive_at": now.isoformat(),
            "pid": os.getpid(),
            "version": __version__,
            "hung": bool(problem),
            "hung_reason": problem or None,
            "recoveries": self.recoveries,
            **self._loop.snapshot(),
        }
        if self._extra is not None:
            try:
                snapshot.update(await self._extra())
            except Exception as error:  # alive_at важнее подробностей: файл пишем всё равно
                logger.warning("дополнение status.json не собрано: %s", type(error).__name__)
        self._journal.status(snapshot)

    async def _recover(self, problem: str) -> None:
        self.recoveries += 1
        self._last_recovery = self._clock.monotonic()
        logger.error("цикл опроса завис: %s; перезапуск", problem)
        self._journal.event("liveness.hung", f"цикл опроса завис: {problem}; перезапуск", "ERROR")
        try:
            await self._notify(f"⚠️ Цикл опроса завис ({problem}). Перезапускаю его и браузер.")
        except Exception as error:  # сообщение не должно мешать восстановлению
            logger.warning("сообщение о зависании не отправлено: %s", type(error).__name__)
        try:
            await self._restart_poll()
        except Exception as error:  # монитор должен пережить неудачный перезапуск
            logger.error("перезапуск цикла опроса не удался: %s", type(error).__name__)
            self._journal.event(
                "liveness.restart_failed",
                f"перезапуск цикла опроса не удался: {type(error).__name__}",
                "ERROR",
            )

    async def run(self) -> None:
        while True:
            await self.tick()
            await self._clock.sleep(STATUS_EVERY_SEC)
