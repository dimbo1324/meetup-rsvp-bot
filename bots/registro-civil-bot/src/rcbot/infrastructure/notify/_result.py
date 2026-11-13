"""Общая сборка `DeliveryResult` каналов: замер времени и сетевые сбои как текст ошибки."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import aiohttp

from rcbot.domain.models import Alert, Channel, DeliveryResult


async def error_of(send: Callable[[], Awaitable[str]]) -> str:
    """Текст ошибки вызова `send`: его собственный или имя сетевого сбоя (таймаут, обрыв)."""
    try:
        return await send()
    except (aiohttp.ClientError, TimeoutError) as exc:
        return type(exc).__name__


async def measured(
    channel: Channel, alert: Alert, attempt: int, send: Callable[[], Awaitable[str]]
) -> DeliveryResult:
    """Выполняет `send` (возвращает текст ошибки, пустой — успех) и собирает результат.

    Сетевой сбой и таймаут — обычный отказ канала, а не исключение: оркестратор рассылки
    должен получить результат каждого канала. Прочие исключения — ошибки кода, они не глушатся.
    """
    started = time.perf_counter()
    error = await error_of(send)
    latency = (time.perf_counter() - started) * 1000
    return DeliveryResult(channel, not error, latency, attempt, error, alert.incident_id)
