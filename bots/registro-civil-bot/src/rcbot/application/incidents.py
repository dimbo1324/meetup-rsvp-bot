"""Учёт инцидентов в БД — отдельно от рассылки: тревога уходит раньше записи (SPEC 2.2, п. 4).

Подтверждение «Вижу» относится к текущему открытому инциденту: кнопка отправляется раньше, чем
БД выдаст номер, поэтому номер в кнопку не кладётся.
"""

from __future__ import annotations

import asyncio
import logging

from rcbot.application.ports import Clock, IncidentRepository
from rcbot.domain.events import SlotsAppeared, SlotsChanged, SlotsGone
from rcbot.domain.models import DeliveryResult, Incident

logger = logging.getLogger(__name__)


class IncidentBook:
    def __init__(self, repository: IncidentRepository, clock: Clock, target_id: int) -> None:
        self._repo = repository
        self._clock = clock
        self._target_id = target_id
        self._current: asyncio.Future[Incident] | None = None
        self._lock = asyncio.Lock()

    async def restore(self) -> Incident | None:
        incident = await self._repo.current(self._target_id)
        if incident is not None:
            restored: asyncio.Future[Incident] = asyncio.get_running_loop().create_future()
            restored.set_result(incident)
            self._current = restored
        return incident

    async def current(self) -> Incident | None:
        if self._current is None:
            return None
        return await self._current

    async def on_appeared(self, event: SlotsAppeared) -> None:
        async with self._lock:
            snapshot = event.snapshot
            self._current = asyncio.create_task(
                self._repo.open(self._target_id, snapshot.observed_at, snapshot.slots)
            )
        incident = await self._current
        logger.info("инцидент %d открыт: %d слотов", incident.id, len(snapshot.slots))

    async def on_changed(self, event: SlotsChanged) -> None:
        incident = await self.current()
        if incident is not None:
            await self._repo.update_slots(incident.id, event.snapshot.slots)
            incident.slots = event.snapshot.slots

    async def on_gone(self, event: SlotsGone) -> None:
        async with self._lock:
            incident = await self.current()
            self._current = None
        if incident is not None:
            await self._repo.close(incident.id, event.at)
            logger.info("инцидент %d закрыт: мест нет %s", incident.id, event.at.isoformat())

    async def ack(self) -> Incident | None:
        incident = await self.current()
        if incident is None or incident.acked:
            return incident
        incident.acked_at = self._clock.now()
        await self._repo.ack(incident.id, incident.acked_at)
        return incident

    async def record(self, result: DeliveryResult) -> None:
        """Доставка пишется после отправки и после того, как у инцидента появился номер."""
        incident = await self.current() if result.incident_id is None else None
        if incident is not None:
            result = DeliveryResult(
                result.channel,
                result.ok,
                result.latency_ms,
                result.attempt,
                result.error,
                incident.id,
            )
        await self._repo.record_delivery(result, self._clock.now())
