"""Подписчик шины событий: каждое доменное событие — в журнал наблюдения, окна записи — в
`openings.csv` (решение R6). Запись в журнал не задерживает рассылку: работает своей задачей."""

from __future__ import annotations

from rcbot.application.bus import EventBus
from rcbot.application.ports import Clock, Journal, OpeningRecord
from rcbot.domain.events import (
    DomainEvent,
    ManualActionRequired,
    PollerStateChanged,
    SlotsAppeared,
    SlotsChanged,
    SlotsGone,
    SourceSwitched,
    UnconfirmedSlotsSeen,
)
from rcbot.domain.models import iso_days


class JournalFeed:
    def __init__(self, journal: Journal, clock: Clock) -> None:
        self._journal = journal
        self._clock = clock
        self._opened_at: float | None = None

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(DomainEvent, self.on_event)

    async def on_event(self, event: DomainEvent) -> None:
        match event:
            case SlotsAppeared(snapshot=snapshot):
                self._opened_at = self._clock.monotonic()
                days = ", ".join(iso_days(snapshot.slots))
                self._journal.event("slots.appeared", f"появились места: {days}", "WARNING")
                self._journal.opening(
                    OpeningRecord(event.at, "появились", snapshot.slots, snapshot.source)
                )
            case SlotsChanged(snapshot=snapshot, added=added):
                days = ", ".join(iso_days(added))
                self._journal.event("slots.changed", f"добавились места: {days}", "WARNING")
                self._journal.opening(OpeningRecord(event.at, "добавились", added, snapshot.source))
            case SlotsGone(snapshot=snapshot):
                lasted = (
                    None if self._opened_at is None else self._clock.monotonic() - self._opened_at
                )
                self._opened_at = None
                self._journal.event("slots.gone", "места пропали: три проверки подряд без мест")
                self._journal.opening(
                    OpeningRecord(event.at, "пропали", frozenset(), snapshot.source, lasted)
                )
            case PollerStateChanged(old=old, new=new, reason=reason):
                self._journal.event("state.changed", f"{old} → {new}: {reason}", "INFO")
            case SourceSwitched(old=old, new=new, reason=reason):
                self._journal.event("source.switched", f"{old} → {new}: {reason}", "WARNING")
            case ManualActionRequired(reason=reason):
                self._journal.event(
                    "manual.required", f"нужен шаг 1 владельцем: {reason}", "WARNING"
                )
            case UnconfirmedSlotsSeen(snapshot=snapshot):
                self._journal.event(
                    "sae.seen", f"SAE: {len(snapshot.slots)} мест (неподтверждённые)"
                )
