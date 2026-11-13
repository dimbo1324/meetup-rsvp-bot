"""Общая сборка результата доставки каналов (DUP-4) и тайм-ауты каналов (MAG-1, COL-1)."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from rcbot.application.alerting import SEND_TIMEOUT_SEC
from rcbot.domain.models import Alert, AlertKind, Channel
from rcbot.infrastructure.notify import ntfy
from rcbot.infrastructure.notify._result import error_of, measured

ALERT = Alert(AlertKind.APPEARED, "т", incident_id=7)


async def test_успех_пустая_ошибка_и_все_поля_результата() -> None:
    async def send() -> str:
        await asyncio.sleep(0.02)
        return ""

    result = await measured(Channel.NTFY, ALERT, 3, send)
    assert (result.channel, result.ok, result.attempt, result.error, result.incident_id) == (
        Channel.NTFY,
        True,
        3,
        "",
        7,
    )
    assert result.latency_ms >= 15


async def test_текст_ошибки_канала_это_отказ() -> None:
    async def send() -> str:
        return "HTTP 500"

    result = await measured(Channel.CALL, ALERT, 1, send)
    assert not result.ok and result.error == "HTTP 500"


@pytest.mark.parametrize(
    "error",
    [aiohttp.ClientConnectionError("x"), aiohttp.ServerDisconnectedError(), TimeoutError()],
)
async def test_сетевой_сбой_это_имя_исключения_а_не_исключение(error: BaseException) -> None:
    async def send() -> str:
        raise error

    result = await measured(Channel.TELEGRAM, ALERT, 2, send)
    assert not result.ok and result.error == type(error).__name__
    assert await error_of(send) == type(error).__name__


async def test_ошибка_кода_не_глушится() -> None:
    async def send() -> str:
        raise ValueError("баг")

    with pytest.raises(ValueError, match="баг"):
        await measured(Channel.NTFY, ALERT, 1, send)


def test_тайм_аут_ntfy_меньше_лимита_оркестратора() -> None:
    assert ntfy.NTFY_TIMEOUT_SEC == 10 < SEND_TIMEOUT_SEC
