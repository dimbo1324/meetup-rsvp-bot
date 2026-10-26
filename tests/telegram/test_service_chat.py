"""Проверка служебного чата при запуске: публичный чат и недоступный чат."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import ClientDecodeError, TelegramBadRequest
from aiogram.methods import GetChat

from meetup_bot.telegram.service_chat import check_service_chat
from meetup_bot.texts.service import SERVICE_CHAT_PUBLIC_TEXT, SERVICE_CHAT_UNKNOWN_TEXT
from tests.helpers import App
from tests.tg_fake import Harness


def chat_info(chat_type: str, username: str | None = None) -> Any:
    return SimpleNamespace(type=chat_type, username=username)


def stub_get_chat(tg: Harness, monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    async def get_chat(chat_id: int) -> Any:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tg.bot, "get_chat", get_chat)


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_закрытая_группа_не_вызывает_предупреждений(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch, chat_type: str
) -> None:
    stub_get_chat(tg, monkeypatch, chat_info(chat_type))
    assert await check_service_chat(tg.bot, app.notifier, -100500) is True
    assert app.notifier.service == []


@pytest.mark.parametrize(
    "info",
    [chat_info("supergroup", "open_chat"), chat_info("channel"), chat_info("private")],
)
async def test_публичный_чат_или_не_группа_даёт_предупреждение(
    app: App,
    tg: Harness,
    monkeypatch: pytest.MonkeyPatch,
    info: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stub_get_chat(tg, monkeypatch, info)
    assert await check_service_chat(tg.bot, app.notifier, -100500) is False
    assert app.notifier.service == [SERVICE_CHAT_PUBLIC_TEXT]
    assert any(r.levelname == "WARNING" for r in caplog.records)


async def test_недоступный_чат_даёт_ошибку_в_лог_и_сообщение(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stub_get_chat(
        tg, monkeypatch, TelegramBadRequest(GetChat(chat_id=1), "Bad Request: chat not found")
    )
    assert await check_service_chat(tg.bot, app.notifier, -100500) is False
    assert app.notifier.service == [SERVICE_CHAT_UNKNOWN_TEXT]
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_сбой_разбора_ответа_не_мешает_запуску(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Диагностика чата вспомогательная: нетипичная ошибка библиотеки только логируется
    broken = ClientDecodeError("не разобран ответ", ValueError("поле"), "{}")
    stub_get_chat(tg, monkeypatch, broken)
    assert await check_service_chat(tg.bot, app.notifier, -100500) is False
    assert app.notifier.service == [SERVICE_CHAT_UNKNOWN_TEXT]
    assert any(r.levelname == "ERROR" for r in caplog.records)
