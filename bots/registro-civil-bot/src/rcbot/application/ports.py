"""Порты прикладного слоя (SPEC 2.3): прикладная логика зависит только от этих протоколов."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from rcbot.domain.models import (
    Alert,
    AppEvent,
    AvailabilitySnapshot,
    Channel,
    DeliveryResult,
    Incident,
    PollStatsHour,
    Slot,
    SourceKind,
    WatchTarget,
)


class Clock(Protocol):
    def now(self) -> datetime:
        """Текущий момент, aware-datetime в UTC."""
        ...

    def monotonic(self) -> float:
        """Монотонное время в секундах (`loop.time()`): от него считается расписание."""
        ...

    async def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class SessionCookie:
    """Cookie сессии. Живёт только в памяти; значение никогда не логируется."""

    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = True

    def __repr__(self) -> str:
        return f"SessionCookie(name={self.name!r}, domain={self.domain!r}, value=***)"


class AvailabilitySource(Protocol):
    @property
    def kind(self) -> SourceKind: ...

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot: ...

    def use_cookies(self, cookies: Sequence[SessionCookie]) -> None: ...

    async def close(self) -> None: ...


class Authenticator(Protocol):
    async def login(self) -> list[SessionCookie]: ...


class Notifier(Protocol):
    @property
    def channel(self) -> Channel: ...

    async def send(self, alert: Alert, attempt: int) -> DeliveryResult: ...

    async def warmup(self) -> None: ...

    async def close(self) -> None: ...


class IncidentRepository(Protocol):
    async def open(
        self, target_id: int, opened_at: datetime, slots: frozenset[Slot]
    ) -> Incident: ...

    async def update_slots(self, incident_id: int, slots: frozenset[Slot]) -> None: ...

    async def close(self, incident_id: int, closed_at: datetime) -> None: ...

    async def ack(self, incident_id: int, acked_at: datetime) -> None: ...

    async def current(self, target_id: int) -> Incident | None: ...

    async def last(self, limit: int) -> list[Incident]: ...

    async def record_delivery(self, result: DeliveryResult, at: datetime) -> None: ...

    async def deliveries_for(self, incident_id: int) -> list[DeliveryResult]: ...


class StatsRepository(Protocol):
    async def save_hour(self, stats: PollStatsHour) -> None: ...

    async def since(self, since: datetime) -> list[PollStatsHour]: ...


class EventLog(Protocol):
    async def write(self, event: AppEvent) -> None: ...

    async def last(self, limit: int, min_level: str = "WARNING") -> list[AppEvent]: ...


class KeyValueStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def all(self) -> dict[str, str]: ...


class TargetRepository(Protocol):
    async def ensure(
        self, name: str, agenda_url: str, location_text: str, months_ahead: int
    ) -> WatchTarget: ...


class Maintenance(Protocol):
    async def delete_older_than(self, cutoffs: dict[str, datetime]) -> dict[str, int]: ...

    async def compact(self) -> None: ...


class FileStore(Protocol):
    """Файлы бота на диске: дампы, карантин, снимки, временные данные, логи."""

    def prune(
        self, name: str, max_age: timedelta, now: datetime, max_files: int | None = None
    ) -> int: ...

    def clear(self, name: str) -> int: ...

    def prune_logs(self, max_age: timedelta, now: datetime) -> int: ...

    def folder_size(self) -> int: ...


@dataclass(frozen=True)
class CheckRecord:
    """Итог одной проверки календаря для журнала наблюдения (решение R6)."""

    at: datetime
    source: SourceKind
    ok: bool
    slots: frozenset[Slot]
    latency_ms: float
    state: str
    interval_sec: float
    error_kind: str = ""
    error: str = ""


@dataclass(frozen=True)
class OpeningRecord:
    """Появление, изменение или пропажа мест — строка статистики окон записи."""

    at: datetime
    kind: str
    slots: frozenset[Slot]
    source: SourceKind
    incident_open_sec: float | None = None


class Journal(Protocol):
    """Журнал наблюдения: каждая проверка, каждое действие, окна записи, текущее состояние."""

    def check(self, record: CheckRecord) -> None: ...

    def event(self, code: str, message: str, level: str = "INFO", **data: object) -> None: ...

    def opening(self, record: OpeningRecord) -> None: ...

    def status(self, snapshot: dict[str, object]) -> None: ...

    def enforce_quota(self) -> tuple[int, int]: ...

    def size(self) -> int: ...


class NullJournal:
    """Журнал выключен (RCBOT_JOURNAL_ENABLED=false) или не нужен команде CLI."""

    def check(self, record: CheckRecord) -> None:
        """Ничего не пишет."""

    def event(self, code: str, message: str, level: str = "INFO", **data: object) -> None:
        """Ничего не пишет."""

    def opening(self, record: OpeningRecord) -> None:
        """Ничего не пишет."""

    def status(self, snapshot: dict[str, object]) -> None:
        """Ничего не пишет."""

    def enforce_quota(self) -> tuple[int, int]:
        return 0, 0

    def size(self) -> int:
        return 0


class WatchdogPinger(Protocol):
    async def ping(self) -> bool: ...


class Coordinator(Protocol):
    """Точка расширения: координация нескольких ноутбуков (SPEC 3.1). В v1 — заглушка."""

    async def should_alert(self, fingerprint: str) -> bool: ...


class NoopCoordinator:
    async def should_alert(self, fingerprint: str) -> bool:
        return True
