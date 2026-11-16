"""Живость бота (решение R6): «жив / завис» видно в status.json журнала и в Telegram.

Раз в 30 секунд обновляется `status.json` с меткой `alive_at`. Цикл опроса считается зависшим,
если проверка идёт дольше `HUNG_CHECK_SEC` или следующая проверка опоздала больше чем на
`HUNG_LATE_SEC`. Тогда: запись в журнал, сообщение владельцу, перезапуск цикла опроса и
источника (соединение закрывается и создаётся заново). Если сам процесс встанет, `alive_at`
перестанет обновляться — это видно по файлу, а внешний «сторож» сообщит о простое.

Сон ноутбука не считается зависанием: после пробуждения цикл опроса «опаздывает» на часы, но это
сон, а не поломка. Монитор замечает его (`SleepWatch`), пишет в журнал и `status.json` и присылает
владельцу «ноутбук спал с … до …», как только после пробуждения проходит первая проверка: до этого
сети может ещё не быть. Не доставленное сообщение повторяется на следующих тиках.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from rcbot import __version__
from rcbot.application.messages import MessageComposer
from rcbot.application.poll_loop import FETCH_TIMEOUT_SEC, PollLoop
from rcbot.application.ports import Clock, Journal
from rcbot.application.sleep_watch import (
    SLEEP_NOTICE_MAX_TRIES,
    SLEEP_NOTICE_MIN_SEC,
    SleepNotice,
    SleepSpan,
    SleepWatch,
)

logger = logging.getLogger(__name__)

STATUS_EVERY_SEC = 30.0
# Проверка прерывается сама через FETCH_TIMEOUT_SEC: порог выше, иначе монитор перезапускал бы
# цикл, пока проверка ещё законно идёт
HUNG_MARGIN_SEC = 90.0
HUNG_CHECK_SEC = FETCH_TIMEOUT_SEC + HUNG_MARGIN_SEC
HUNG_LATE_SEC = 180.0
# Если после перезапуска цикл всё ещё завис, лечим снова не чаще, чем раз в это время
HUNG_RETRY_SEC = 300.0


class LivenessMonitor:
    def __init__(
        self,
        loop: PollLoop,
        journal: Journal,
        clock: Clock,
        notify: Callable[[str], Awaitable[object]],
        restart_poll: Callable[[], Awaitable[None]],
        extra: Callable[[], Awaitable[dict[str, object]]] | None = None,
        *,
        sleep: SleepWatch | None = None,
        tz: ZoneInfo | None = None,
    ) -> None:
        self._loop = loop
        self._journal = journal
        self._clock = clock
        self._notify = notify
        self._restart_poll = restart_poll
        self._extra = extra
        self._sleep = sleep
        self._tz = tz or ZoneInfo("UTC")
        self._notice: SleepNotice | None = None
        self.hung_since: datetime | None = None
        self.recoveries = 0
        self.sleeps_total_sec = 0.0
        self.last_sleep: SleepSpan | None = None
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

    def _note_sleep(self, span: SleepSpan) -> None:
        self.sleeps_total_sec += span.slept_sec
        self.last_sleep = span
        self._journal.event(
            "system.sleep",
            f"ноутбук спал {span.slept_sec:.0f} с: проверки не шли",
            "WARNING",
            started=span.started.isoformat(),
            ended=span.ended.isoformat(),
            slept_sec=round(span.slept_sec),
        )
        logger.warning("ноутбук спал %.0f с: проверки не шли", span.slept_sec)
        if span.slept_sec < SLEEP_NOTICE_MIN_SEC:
            return
        if self._notice is None:
            self._notice = SleepNotice(span.started, span.ended, span.slept_sec)
        else:
            self._notice.merge(span)

    async def _send_sleep_notice(self) -> None:
        """После пробуждения ждём первую успешную проверку: она значит, что сеть вернулась."""
        notice = self._notice
        if notice is None:
            return
        checked = self._loop.status.last_check_at
        if checked is None or checked < notice.ended:
            return
        notice.tries += 1
        text = MessageComposer.slept(notice.started, notice.ended, notice.slept_sec, self._tz)
        try:
            delivered = await self._notify(text) is not False
        except Exception as error:  # сообщение не должно ронять монитор
            logger.warning("сообщение о сне не отправлено: %s", type(error).__name__)
            delivered = False
        if delivered or notice.tries >= SLEEP_NOTICE_MAX_TRIES:
            if not delivered:
                self._journal.event(
                    "system.sleep_unreported", "сообщение о сне не доставлено", "WARNING"
                )
            self._notice = None

    async def tick(self) -> None:
        span = self._sleep.poll() if self._sleep is not None else None
        if span is not None:
            self._note_sleep(span)
        # В тик пробуждения цикл опроса ещё не успел проверить: его «опоздание» — это сон
        problem = "" if span is not None else self.diagnose()
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
            "sleeps_total_sec": round(self.sleeps_total_sec),
            "last_sleep_started": self.last_sleep.started.isoformat() if self.last_sleep else None,
            "last_sleep_sec": round(self.last_sleep.slept_sec) if self.last_sleep else None,
            **self._loop.snapshot(),
        }
        if self._extra is not None:
            try:
                snapshot.update(await self._extra())
            except Exception as error:  # alive_at важнее подробностей: файл пишем всё равно
                logger.warning("дополнение status.json не собрано: %s", type(error).__name__)
        self._journal.status(snapshot)
        await self._send_sleep_notice()

    async def _recover(self, problem: str) -> None:
        self.recoveries += 1
        self._last_recovery = self._clock.monotonic()
        logger.error("цикл опроса завис: %s; перезапуск", problem)
        self._journal.event("liveness.hung", f"цикл опроса завис: {problem}; перезапуск", "ERROR")
        try:
            await self._notify(MessageComposer.poll_hung(problem))
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
