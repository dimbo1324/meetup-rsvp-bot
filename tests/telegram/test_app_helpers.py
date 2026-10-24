"""Вспомогательные функции запуска приложения: чистка токена, путь к SQLite, Sentry, сторож."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from meetup_bot import app as app_module
from meetup_bot.app import (
    _init_sentry,
    _scrub,
    _sentry_before_breadcrumb,
    _sentry_before_send,
    _sqlite_path,
    _watch_scheduler,
)

TOKEN = "123456789:AAH-secret_Token-xyz"


def test_scrub_вырезает_токен_из_строки() -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/getMe"
    assert _scrub(url) == "https://api.telegram.org/bot<скрыто>/getMe"


def test_scrub_вырезает_токен_из_вложенных_структур() -> None:
    event = {
        "request": {"url": f"https://api.telegram.org/bot{TOKEN}/sendMessage"},
        "breadcrumbs": [{"message": f"POST /bot{TOKEN}/x"}, {"data": [f"bot{TOKEN}"]}],
    }
    scrubbed = _scrub(event)
    assert TOKEN not in repr(scrubbed)
    assert scrubbed["request"]["url"].endswith("/bot<скрыто>/sendMessage")
    assert scrubbed["breadcrumbs"][1]["data"] == ["bot<скрыто>"]


def test_scrub_не_трогает_обычные_значения() -> None:
    event = {"n": 5, "ok": True, "none": None, "text": "обычный текст", "list": [1, "a"]}
    assert _scrub(event) == event


def test_scrub_вырезает_несколько_токенов_в_одной_строке() -> None:
    text = f"bot{TOKEN} и ещё bot111:abc_DEF"
    assert _scrub(text) == "bot<скрыто> и ещё bot<скрыто>"


def test_scrub_не_мутирует_исходную_структуру() -> None:
    original = {"url": f"/bot{TOKEN}/x"}
    _scrub(original)
    assert TOKEN in original["url"]


def test_хуки_sentry_чистят_событие_и_хлебные_крошки() -> None:
    crumb = {"message": f"bot{TOKEN}"}
    assert _sentry_before_send(crumb, None) == {"message": "bot<скрыто>"}
    assert _sentry_before_breadcrumb(crumb, None) == {"message": "bot<скрыто>"}


def test_sqlite_path_для_относительного_пути() -> None:
    assert _sqlite_path("sqlite+aiosqlite:///data/bot.db") == Path("data/bot.db")


def test_sqlite_path_для_абсолютного_пути() -> None:
    assert _sqlite_path("sqlite+aiosqlite:////var/lib/bot.db") == Path("/var/lib/bot.db")


@pytest.mark.parametrize(
    "url", ["postgresql+asyncpg://u@h/db", "sqlite:///x.db", "sqlite+aiosqlite://", ""]
)
def test_sqlite_path_отвергает_чужие_url(url: str) -> None:
    with pytest.raises(ValueError, match="SQLite"):
        _sqlite_path(url)


def test_init_sentry_без_dsn_ничего_не_делает(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ModuleType("sentry_sdk")
    called: list[Any] = []
    fake.init = lambda **kwargs: called.append(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    _init_sentry("")
    assert called == []


def test_init_sentry_с_dsn_включает_защитные_настройки(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ModuleType("sentry_sdk")
    called: list[dict[str, Any]] = []
    fake.init = lambda **kwargs: called.append(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    _init_sentry("https://key@example.invalid/1")
    (kwargs,) = called
    assert kwargs["send_default_pii"] is False
    assert kwargs["include_local_variables"] is False
    assert kwargs["before_send"] is app_module._sentry_before_send
    assert kwargs["before_breadcrumb"] is app_module._sentry_before_breadcrumb


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_service(self, text: str) -> None:
        self.messages.append(text)


async def test_упавший_планировщик_сообщает_в_служебный_чат() -> None:
    async def crash() -> None:
        raise RuntimeError("сломался")

    notifier = RecordingNotifier()
    task = asyncio.create_task(crash())
    _watch_scheduler(task, notifier)  # type: ignore[arg-type]
    await asyncio.wait([task])
    await asyncio.sleep(0.05)
    assert notifier.messages == ["⚠️ Планировщик остановился: RuntimeError"]


async def test_отменённый_планировщик_не_поднимает_тревогу() -> None:
    async def forever() -> None:
        await asyncio.sleep(60)

    notifier = RecordingNotifier()
    task = asyncio.create_task(forever())
    _watch_scheduler(task, notifier)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait([task])
    await asyncio.sleep(0.05)
    assert notifier.messages == []


async def test_штатно_завершённый_планировщик_не_поднимает_тревогу() -> None:
    async def finish() -> None:
        return None

    notifier = RecordingNotifier()
    task = asyncio.create_task(finish())
    _watch_scheduler(task, notifier)  # type: ignore[arg-type]
    await asyncio.wait([task])
    await asyncio.sleep(0.05)
    assert notifier.messages == []
