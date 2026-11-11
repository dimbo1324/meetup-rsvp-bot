"""Системные часы: UTC для меток и `loop.time()` для расписания без дрейфа (SPEC 2.2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
