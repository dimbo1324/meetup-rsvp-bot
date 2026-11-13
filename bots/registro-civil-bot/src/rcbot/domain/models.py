"""Доменные сущности SPEC 3.1. Только стандартная библиотека: слой не знает о сети, БД, Telegram."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum
from functools import total_ordering


class SourceKind(StrEnum):
    API = "api"
    DOM = "dom"
    SAE = "sae"


class Channel(StrEnum):
    TELEGRAM = "telegram"
    NTFY = "ntfy"
    CALL = "call"


class PollState(StrEnum):
    RUNNING = "running"
    REAUTH = "reauth"
    BACKOFF = "backoff"
    NEEDS_MANUAL = "needs_manual"
    PAUSED = "paused"


class AlertKind(StrEnum):
    APPEARED = "appeared"
    CHANGED = "changed"
    SERVICE = "service"
    UNCONFIRMED = "unconfirmed"
    TEST = "test"


class IncidentStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class WatchTarget:
    id: int
    name: str
    agenda_url: str
    location_text: str
    months_ahead: int


@total_ordering
@dataclass(frozen=True)
class Slot:
    """Свободное место; время неизвестно, если источник видит только день.

    Порядок: день, затем время; слот «только день» идёт раньше слотов со временем того же дня
    (встроенное сравнение кортежей падало бы на `None < time`).
    """

    day: date
    at: time | None = None

    def _sort_key(self) -> tuple[date, bool, time]:
        return (self.day, self.at is not None, self.at or time.min)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Slot):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    def label(self) -> str:
        return (
            self.day.isoformat() if self.at is None else f"{self.day.isoformat()} {self.at:%H:%M}"
        )


def iso_days(slots: frozenset[Slot]) -> list[str]:
    """Дни слотов без повторов, по порядку, в ISO-формате: время слота здесь не важно."""
    return sorted({slot.day.isoformat() for slot in slots})


def percentile(values: list[float], q: float) -> float:
    """Перцентиль по ближайшему рангу; без данных — 0, а не исключение."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def fingerprint_of(slots: frozenset[Slot]) -> str:
    joined = "|".join(slot.label() for slot in sorted(slots))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class AvailabilitySnapshot:
    target_id: int
    observed_at: datetime
    slots: frozenset[Slot]
    source: SourceKind
    latency_ms: float
    fingerprint: str = field(default="")

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at должен быть aware-datetime в UTC")
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", fingerprint_of(self.slots))

    @property
    def has_slots(self) -> bool:
        return bool(self.slots)


@dataclass
class Incident:
    id: int
    target_id: int
    opened_at: datetime
    slots: frozenset[Slot]
    status: IncidentStatus = IncidentStatus.OPEN
    closed_at: datetime | None = None
    acked_at: datetime | None = None

    @property
    def acked(self) -> bool:
        return self.acked_at is not None


class AlertAction(StrEnum):
    """Кнопка под сообщением в Telegram."""

    NONE = "none"
    ACK = "ack"
    MANUAL_DONE = "manual_done"


@dataclass(frozen=True)
class Alert:
    kind: AlertKind
    text: str
    call_text: str = ""
    # Запасной текст звонка на другом голосе, если основной не принят; пусто — без запасного
    call_fallback_text: str = ""
    push_title: str = ""
    push_text: str = ""
    action: AlertAction = AlertAction.NONE
    incident_id: int | None = None
    # Сервисные и неподтверждённые сообщения идут только в Telegram, без звонка (SPEC 2.3–2.4)
    channels: frozenset[Channel] = frozenset(Channel)


@dataclass(frozen=True)
class DeliveryResult:
    channel: Channel
    ok: bool
    latency_ms: float
    attempt: int
    error: str = ""
    incident_id: int | None = None


@dataclass
class PollStatsHour:
    hour: datetime
    checks: int = 0
    errors: int = 0
    relogins: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def percentile(self, q: float) -> float:
        return percentile(self.latencies_ms, q)


@dataclass(frozen=True)
class AppEvent:
    ts: datetime
    level: str
    code: str
    message: str
