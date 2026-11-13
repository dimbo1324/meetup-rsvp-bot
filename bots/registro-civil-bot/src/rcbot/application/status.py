"""Сводка состояния для `/status`, меню и ежедневного отчёта (SPEC 2.5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rcbot.domain.models import PollState, SourceKind


@dataclass
class PollStatus:
    started_at: datetime
    state: PollState = PollState.RUNNING
    source: SourceKind = SourceKind.DOM
    interval_sec: float = 0.0
    last_check_at: datetime | None = None
    last_latency_ms: float | None = None
    last_error: str = ""
    last_error_at: datetime | None = None
    checks_total: int = 0
    errors_total: int = 0
    state_reason: str = ""
    recent_latencies: list[float] = field(default_factory=list)

    def note_latency(self, latency_ms: float, keep: int = 200) -> None:
        self.recent_latencies.append(latency_ms)
        del self.recent_latencies[:-keep]
