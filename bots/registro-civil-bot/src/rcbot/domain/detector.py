"""Детектор (SPEC 2.3, модель инцидента): сравнение снимков без побочных эффектов.

- Инцидент открывается, когда множество слотов стало непустым → `SlotsAppeared`.
- Новые слоты в открытом инциденте → `SlotsChanged` (новое уведомление). Пропажа части слотов
  тревоги не вызывает: множество обновляется молча.
- Инцидент закрывается после трёх пустых проверок подряд → `SlotsGone` (защита от дребезга).
- Снимок с тем же отпечатком, что и прошлый непустой, дальше не обрабатывается (SPEC 2.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rcbot.domain.events import DomainEvent, SlotsAppeared, SlotsChanged, SlotsGone
from rcbot.domain.models import AvailabilitySnapshot, Slot

EMPTY_CHECKS_TO_CLOSE = 3


@dataclass
class Detector:
    empty_checks_to_close: int = EMPTY_CHECKS_TO_CLOSE
    slots: frozenset[Slot] = field(default_factory=frozenset)
    incident_open: bool = False
    empty_streak: int = 0
    last_fingerprint: str = ""

    def restore(self, slots: frozenset[Slot]) -> None:
        """Продолжение открытого инцидента после перезапуска: старые слоты не тревожат заново."""
        self.slots = slots
        self.incident_open = bool(slots)
        self.empty_streak = 0
        self.last_fingerprint = ""

    def compare(self, snapshot: AvailabilitySnapshot) -> DomainEvent | None:
        if snapshot.has_slots:
            self.empty_streak = 0
            if self.incident_open and snapshot.fingerprint == self.last_fingerprint:
                return None
            self.last_fingerprint = snapshot.fingerprint
            if not self.incident_open:
                self.incident_open = True
                self.slots = snapshot.slots
                return SlotsAppeared(snapshot.observed_at, snapshot)
            added = snapshot.slots - self.slots
            self.slots = snapshot.slots
            if added:
                return SlotsChanged(snapshot.observed_at, snapshot, frozenset(added))
            return None

        self.last_fingerprint = ""
        if not self.incident_open:
            return None
        self.empty_streak += 1
        if self.empty_streak < self.empty_checks_to_close:
            return None
        self.incident_open = False
        self.slots = frozenset()
        self.empty_streak = 0
        return SlotsGone(snapshot.observed_at, snapshot)
