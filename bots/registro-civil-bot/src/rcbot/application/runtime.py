"""Настройки, меняемые из Telegram (таблица `kv_settings`), и лестница частоты опроса (D4).

Нижняя граница интервала зашита в `config.settings` и проверяется здесь ещё раз: ни `.env`, ни
Telegram не могут задать меньше 10 с (SPEC 0.3, правило 3; критерий приёмки 9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rcbot.application.ports import KeyValueStore
from rcbot.config.settings import MAX_POLL_INTERVAL_SEC, MIN_POLL_INTERVAL_SEC
from rcbot.domain.models import Channel

# D4: первые 6 часов — 60 с, следующие 12 часов — 30 с, дальше — базовый интервал
LADDER: tuple[tuple[timedelta, float], ...] = (
    (timedelta(hours=6), 60.0),
    (timedelta(hours=12), 30.0),
)

K_INTERVAL = "poll_interval_sec"
K_PAUSED_UNTIL = "paused_until"
K_MONTHS = "months_ahead"
K_CHANNELS_OFF = "channels_off"
K_LADDER_START = "ladder_started_at"


def clamp_interval(seconds: float) -> float:
    return min(max(seconds, MIN_POLL_INTERVAL_SEC), MAX_POLL_INTERVAL_SEC)


@dataclass
class RuntimeSettings:
    base_interval_sec: float
    months_ahead: int
    ladder_started_at: datetime
    paused_until: datetime | None = None
    channels_off: set[Channel] = field(default_factory=set)

    def ladder_interval(self, now: datetime) -> float:
        elapsed = now - self.ladder_started_at
        bound = timedelta(0)
        for length, interval in LADDER:
            bound += length
            if elapsed < bound:
                return max(interval, self.base_interval_sec)
        return self.base_interval_sec

    def step_back(self, now: datetime) -> None:
        """403/429: на ступень медленнее (D4) — лестница отматывается к началу прошлой ступени."""
        elapsed = now - self.ladder_started_at
        starts = [timedelta(0)]
        for length, _ in LADDER:
            starts.append(starts[-1] + length)
        current = max(i for i, start in enumerate(starts) if elapsed >= start)
        self.ladder_started_at = now - starts[max(current - 1, 0)]

    def paused(self, now: datetime) -> bool:
        return self.paused_until is not None and now < self.paused_until

    def channel_enabled(self, channel: Channel) -> bool:
        return channel not in self.channels_off


class RuntimeStore:
    def __init__(self, kv: KeyValueStore) -> None:
        self._kv = kv

    async def load(
        self, default_interval: float, default_months: int, now: datetime
    ) -> RuntimeSettings:
        values = await self._kv.all()
        started = values.get(K_LADDER_START)
        settings = RuntimeSettings(
            base_interval_sec=clamp_interval(float(values.get(K_INTERVAL, default_interval))),
            months_ahead=int(values.get(K_MONTHS, default_months)),
            ladder_started_at=datetime.fromisoformat(started) if started else now,
            paused_until=datetime.fromisoformat(values[K_PAUSED_UNTIL])
            if values.get(K_PAUSED_UNTIL)
            else None,
            channels_off={Channel(c) for c in values.get(K_CHANNELS_OFF, "").split(",") if c},
        )
        if not started:
            await self.save(settings)
        return settings

    async def save(self, settings: RuntimeSettings) -> None:
        await self._kv.set(K_INTERVAL, f"{clamp_interval(settings.base_interval_sec):g}")
        await self._kv.set(K_MONTHS, str(settings.months_ahead))
        await self._kv.set(K_LADDER_START, settings.ladder_started_at.isoformat())
        paused = settings.paused_until.isoformat() if settings.paused_until else ""
        await self._kv.set(K_PAUSED_UNTIL, paused)
        await self._kv.set(K_CHANNELS_OFF, ",".join(sorted(c.value for c in settings.channels_off)))
