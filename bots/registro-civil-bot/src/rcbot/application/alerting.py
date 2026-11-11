"""Рассылка тревог (SPEC 2.4, 5): все каналы параллельно, повторы до «Вижу», прогрев соединений.

Первая волна уходит сразу из обработчика события, до записи в БД. У каждого канала свой таймаут
и свои повторы; сбой одного не задерживает остальные. «Вижу» (или пропажа мест) останавливает
повторы во всех каналах.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from typing import Any

from rcbot.application.incidents import IncidentBook
from rcbot.application.messages import MessageComposer
from rcbot.application.ports import Clock, Coordinator, Notifier
from rcbot.application.runtime import RuntimeSettings
from rcbot.application.session_manager import SessionManager
from rcbot.domain.events import (
    ManualActionRequired,
    PollerStateChanged,
    SlotsAppeared,
    SlotsChanged,
    SlotsGone,
    SourceSwitched,
    UnconfirmedSlotsSeen,
)
from rcbot.domain.models import Alert, Channel, DeliveryResult, PollState

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SEC = 12.0
WARMUP_EVERY_SEC = 240.0


@dataclass(frozen=True)
class RepeatPolicy:
    every_sec: float
    max_repeats: int


class AlertOrchestrator:
    def __init__(
        self,
        notifiers: dict[Channel, Notifier],
        policies: dict[Channel, RepeatPolicy],
        composer: MessageComposer,
        book: IncidentBook,
        session: SessionManager,
        runtime: RuntimeSettings,
        clock: Clock,
        coordinator: Coordinator,
    ) -> None:
        self._notifiers = notifiers
        self._policies = policies
        self._composer = composer
        self._book = book
        self._session = session
        self._runtime = runtime
        self._clock = clock
        self._coordinator = coordinator
        self._repeats: list[asyncio.Task[None]] = []
        self._background: set[asyncio.Task[None]] = set()
        self._stopped = asyncio.Event()
        self.on_delivery: Callable[[DeliveryResult], None] | None = None

    # --- события ---

    async def on_appeared(self, event: SlotsAppeared) -> None:
        if not await self._coordinator.should_alert(event.snapshot.fingerprint):
            return
        self._session.quiet_after_alert()
        await self._campaign(self._composer.slots_alert(event.snapshot, self._clock.now()))

    async def on_changed(self, event: SlotsChanged) -> None:
        self._session.quiet_after_alert()
        alert = self._composer.slots_alert(event.snapshot, self._clock.now(), added=event.added)
        await self._campaign(alert)

    async def on_gone(self, event: SlotsGone) -> None:
        self._cancel_repeats()
        await self._wave(self._composer.gone(event.at), attempt=1)

    async def on_unconfirmed(self, event: UnconfirmedSlotsSeen) -> None:
        await self._wave(self._composer.unconfirmed_alert(event.snapshot), attempt=1)

    async def on_manual(self, event: ManualActionRequired) -> None:
        await self._wave(self._composer.manual_step(event.reason), attempt=1)

    async def on_source_switched(self, event: SourceSwitched) -> None:
        text = f"🔀 Источник данных: {event.old} → {event.new}. Причина: {event.reason}."
        await self._wave(self._composer.service(text), attempt=1)

    async def on_state_changed(self, event: PollerStateChanged) -> None:
        # NEEDS_MANUAL сообщается отдельным сообщением с кнопкой; REAUTH обычно проходит сам
        if event.new is PollState.BACKOFF:
            text = f"⏸ Бот замедлился: {event.reason}. Опрос продолжится сам."
        elif event.new is PollState.RUNNING and event.old in (
            PollState.BACKOFF,
            PollState.NEEDS_MANUAL,
        ):
            text = "▶️ Опрос снова работает."
        else:
            return
        await self._wave(self._composer.service(text), attempt=1)

    # --- управление ---

    async def ack(self) -> bool:
        """«Вижу»: повторы во всех каналах останавливаются, инцидент остаётся открытым."""
        had_repeats = any(not t.done() for t in self._repeats)
        self._cancel_repeats()
        incident = await self._book.ack()
        return had_repeats or incident is not None

    async def service(self, text: str) -> None:
        await self._wave(self._composer.service(text), attempt=1)

    async def test(self, channels: set[Channel] | None = None) -> list[DeliveryResult]:
        alert = self._composer.test(self._clock.now())
        if channels is not None:
            alert = replace(alert, channels=frozenset(channels))
        return await self._wave(alert, attempt=1, respect_disabled=False)

    async def simulate(self) -> None:
        """`rcbot simulate`: тестовая тревога со всеми повторами, как у настоящей."""
        await self._campaign(self._composer.test(self._clock.now()), record=False)

    def repeating(self) -> bool:
        return any(not t.done() for t in self._repeats)

    # --- рассылка ---

    async def _campaign(self, alert: Alert, *, record: bool = True) -> None:
        self._cancel_repeats()
        await self._wave(alert, attempt=1, record=record)
        for channel in alert.channels:
            policy = self._policies.get(channel)
            if policy and policy.max_repeats > 0 and channel in self._notifiers:
                task = asyncio.create_task(self._repeat(channel, alert, policy, record))
                self._repeats.append(task)

    async def _repeat(
        self, channel: Channel, alert: Alert, policy: RepeatPolicy, record: bool
    ) -> None:
        for attempt in range(2, policy.max_repeats + 2):
            await self._clock.sleep(policy.every_sec)
            if not self._runtime.channel_enabled(channel):
                continue
            result = await self._send_one(self._notifiers[channel], alert, attempt)
            if record:
                self._spawn(self._book.record(result))

    async def _wave(
        self, alert: Alert, attempt: int, *, record: bool = True, respect_disabled: bool = True
    ) -> list[DeliveryResult]:
        targets = [
            self._notifiers[c]
            for c in alert.channels
            if c in self._notifiers and (not respect_disabled or self._runtime.channel_enabled(c))
        ]
        if not targets:
            logger.warning("тревога %s: нет включённых каналов", alert.kind)
            return []
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._send_one(n, alert, attempt)) for n in targets]
        results = [task.result() for task in tasks]
        if record and alert.kind.value in ("appeared", "changed"):
            for result in results:
                self._spawn(self._book.record(result))
        return results

    async def _send_one(self, notifier: Notifier, alert: Alert, attempt: int) -> DeliveryResult:
        try:
            async with asyncio.timeout(SEND_TIMEOUT_SEC):
                result = await notifier.send(alert, attempt)
        except TimeoutError:
            result = DeliveryResult(
                notifier.channel, False, SEND_TIMEOUT_SEC * 1000, attempt, "таймаут"
            )
        except Exception as error:  # канал не должен уронить рассылку по остальным
            logger.exception("канал %s упал", notifier.channel)
            result = DeliveryResult(notifier.channel, False, 0.0, attempt, type(error).__name__)
        if self.on_delivery is not None:
            self.on_delivery(result)
        return result

    def _cancel_repeats(self) -> None:
        for task in self._repeats:
            task.cancel()
        self._repeats.clear()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- прогрев ---

    async def keep_warm(self) -> None:
        """Раз в 4 минуты лёгкий запрос в каждый канал: TCP и TLS уже установлены (SPEC 2.2)."""
        while not self._stopped.is_set():
            for notifier in self._notifiers.values():
                try:
                    async with asyncio.timeout(SEND_TIMEOUT_SEC):
                        await notifier.warmup()
                except Exception as error:  # прогрев — не повод останавливать бота
                    logger.info("прогрев %s не удался: %s", notifier.channel, type(error).__name__)
            await self._clock.sleep(WARMUP_EVERY_SEC)

    async def close(self) -> None:
        self._stopped.set()
        self._cancel_repeats()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
