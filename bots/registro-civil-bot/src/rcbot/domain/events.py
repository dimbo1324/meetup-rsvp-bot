"""Доменные события SPEC 3.1: их публикует цикл опроса, на них подписаны рассылка и журнал."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rcbot.domain.models import AvailabilitySnapshot, PollState, Slot, SourceKind


@dataclass(frozen=True)
class DomainEvent:
    at: datetime


@dataclass(frozen=True)
class SlotsAppeared(DomainEvent):
    snapshot: AvailabilitySnapshot


@dataclass(frozen=True)
class SlotsChanged(DomainEvent):
    snapshot: AvailabilitySnapshot
    added: frozenset[Slot]


@dataclass(frozen=True)
class SlotsGone(DomainEvent):
    snapshot: AvailabilitySnapshot


@dataclass(frozen=True)
class ManualActionRequired(DomainEvent):
    reason: str


@dataclass(frozen=True)
class SourceSwitched(DomainEvent):
    old: SourceKind
    new: SourceKind
    reason: str


@dataclass(frozen=True)
class PollerStateChanged(DomainEvent):
    old: PollState
    new: PollState
    reason: str


@dataclass(frozen=True)
class UnconfirmedSlotsSeen(DomainEvent):
    """Находка вторичного источника (SAE): только сообщение в Telegram, без звонка."""

    snapshot: AvailabilitySnapshot
