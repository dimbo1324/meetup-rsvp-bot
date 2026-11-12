"""Настройки, меняемые из Telegram (таблица `kv_settings`), и лестница частоты опроса (D4).

Нижняя граница интервала зашита в `config.settings` и проверяется здесь ещё раз: ни `.env`, ни
Telegram не могут задать меньше 10 с (SPEC 0.3, правило 3; критерий приёмки 9).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rcbot.application.ports import KeyValueStore
from rcbot.config.settings import (
    MAX_MONTHS_AHEAD,
    MAX_POLL_INTERVAL_SEC,
    MIN_MONTHS_AHEAD,
    MIN_POLL_INTERVAL_SEC,
)
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
    # NaN проходит и min(), и max() без изменений: интервал «не число» — ошибка, а не 0 с
    if math.isnan(seconds):
        raise ValueError("интервал опроса должен быть числом")
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
        # Часы ушли назад: начало лестницы «в будущем» считаем её началом, а не падаем
        elapsed = max(now - self.ladder_started_at, timedelta(0))
        starts = [timedelta(0)]
        for length, _ in LADDER:
            starts.append(starts[-1] + length)
        current = max(i for i, start in enumerate(starts) if elapsed >= start)
        self.ladder_started_at = now - starts[max(current - 1, 0)]

    def paused(self, now: datetime) -> bool:
        return self.paused_until is not None and now < self.paused_until

    def channel_enabled(self, channel: Channel) -> bool:
        return channel not in self.channels_off


def _parse[T](raw: str | None, convert: Callable[[str], T], default: T) -> T:
    """Значение из БД или умолчание: порча `kv_settings` не должна ронять запуск бота."""
    if raw is None:
        return default
    try:
        return convert(raw)
    except ValueError:
        return default


def _moment(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("в БД хранятся только aware-моменты")
    return parsed


def _number(raw: str) -> float:
    value = float(raw)
    if math.isnan(value):
        raise ValueError("не число")
    return value


def _channels(raw: str) -> set[Channel]:
    known = {channel.value: channel for channel in Channel}
    names = (part.strip() for part in raw.split(","))
    return {known[name] for name in names if name in known}


class RuntimeStore:
    def __init__(self, kv: KeyValueStore) -> None:
        self._kv = kv

    async def load(
        self, default_interval: float, default_months: int, now: datetime
    ) -> RuntimeSettings:
        values = await self._kv.all()
        interval = _parse(values.get(K_INTERVAL), _number, default_interval)
        months = _parse(values.get(K_MONTHS), int, default_months)
        stored_start = _parse(values.get(K_LADDER_START), _moment, None)
        # Лестница не может начаться в будущем (часы ушли назад или значение испорчено)
        started = now if stored_start is None else min(stored_start, now)
        settings = RuntimeSettings(
            base_interval_sec=clamp_interval(interval),
            months_ahead=min(max(months, MIN_MONTHS_AHEAD), MAX_MONTHS_AHEAD),
            ladder_started_at=started,
            paused_until=_parse(values.get(K_PAUSED_UNTIL), _moment, None),
            channels_off=_channels(values.get(K_CHANNELS_OFF, "")),
        )
        if started != stored_start:
            await self.save(settings)
        return settings

    async def save(self, settings: RuntimeSettings) -> None:
        await self._kv.set(K_INTERVAL, f"{clamp_interval(settings.base_interval_sec):g}")
        await self._kv.set(K_MONTHS, str(settings.months_ahead))
        await self._kv.set(K_LADDER_START, settings.ladder_started_at.isoformat())
        paused = settings.paused_until.isoformat() if settings.paused_until else ""
        await self._kv.set(K_PAUSED_UNTIL, paused)
        await self._kv.set(K_CHANNELS_OFF, ",".join(sorted(c.value for c in settings.channels_off)))
