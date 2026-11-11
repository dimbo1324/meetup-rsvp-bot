"""Требования к Telegram-слою и запуску, найденные аудитом (код подгоняется под тесты)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramUnauthorizedError
from aiogram.methods import GetMe, SetMyCommands
from pydantic import SecretStr

from meetup_bot import __main__ as entry
from meetup_bot.config import Settings
from meetup_bot.telegram.commands import (
    ADMIN_COMMANDS,
    USER_COMMANDS,
    set_admin_commands,
    sync_commands,
)
from tests.helpers import ADMIN_ID, OWNER_ID, App
from tests.tg_fake import Harness, command_update

TOKEN = "123456:AAEsecretPART-xyz"


# --- секреты и запуск ----------------------------------------------------------------------


def test_токен_не_раскрывается_в_представлении_настроек() -> None:
    config = Settings(
        _env_file=None,  # type: ignore[call-arg]
        bot_token=TOKEN,  # type: ignore[arg-type]
        owner_ids="1",  # type: ignore[arg-type]
        service_chat_id=-1,
    )
    assert isinstance(config.bot_token, SecretStr)
    assert config.bot_token.get_secret_value() == TOKEN
    assert "secretPART" not in repr(config)
    assert "secretPART" not in str(config.model_dump())


def test_неполный_env_даёт_понятное_сообщение_без_трейсбека(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("BOT_TOKEN", "OWNER_IDS", "SERVICE_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level(logging.ERROR):
        code = entry.main()
    assert code == 2
    message = caplog.text
    assert "BOT_TOKEN" in message and "OWNER_IDS" in message and "SERVICE_CHAT_ID" in message
    assert "Traceback" not in message


def test_неверный_токен_даёт_понятное_сообщение(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("OWNER_IDS", "1")
    monkeypatch.setenv("SERVICE_CHAT_ID", "-1")

    async def unauthorized(config: Settings) -> None:
        raise TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")

    monkeypatch.setattr(entry, "run", unauthorized)
    with caplog.at_level(logging.ERROR):
        code = entry.main()
    assert code == 1
    assert "Telegram отклонил BOT_TOKEN" in caplog.text
    assert "secretPART" not in caplog.text


# --- команды по ролям ----------------------------------------------------------------------


def _commands(tg: Harness) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for data in tg.session.of("SetMyCommands"):
        scope = data["scope"]
        key = scope["type"] if scope["type"] != "chat" else f"chat:{scope['chat_id']}"
        result[key] = [c["command"] for c in data["commands"]]
    return result


async def test_меню_админа_видно_только_админам(app: App, tg: Harness) -> None:
    await sync_commands(tg.bot, [OWNER_ID, ADMIN_ID])

    commands = _commands(tg)
    assert commands["all_private_chats"] == [c.command for c in USER_COMMANDS]
    assert "menu" not in commands["all_private_chats"]
    assert commands[f"chat:{OWNER_ID}"] == [c.command for c in ADMIN_COMMANDS]
    assert commands[f"chat:{ADMIN_ID}"] == [c.command for c in ADMIN_COMMANDS]


async def test_недоступный_чат_админа_не_ломает_установку_команд(app: App, tg: Harness) -> None:
    def fail(name: str, data: dict[str, Any]) -> Exception | None:
        if name == "SetMyCommands" and data["scope"].get("chat_id") == ADMIN_ID:
            return TelegramBadRequest(method=SetMyCommands(commands=[]), message="chat not found")
        return None

    tg.session.fail = fail
    await sync_commands(tg.bot, [OWNER_ID, ADMIN_ID])
    assert f"chat:{OWNER_ID}" in _commands(tg)


async def test_снятие_админа_убирает_его_команды(app: App, tg: Harness) -> None:
    await set_admin_commands(tg.bot, ADMIN_ID, is_admin=False)
    deleted = tg.session.of("DeleteMyCommands")
    assert deleted and deleted[-1]["scope"]["chat_id"] == ADMIN_ID


async def test_start_админа_включает_ему_меню_команд(app: App, tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/start"))
    assert f"chat:{OWNER_ID}" in _commands(tg)


# --- команды внутри диалога ----------------------------------------------------------------


async def _open_capacity_input(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = tg.session.last_message_id
    await tg.click(OWNER_ID, mid, "Настройки")
    await tg.click(OWNER_ID, mid, "Встречи по умолчанию")
    await tg.click(OWNER_ID, mid, "Лимит мест")


async def test_start_внутри_диалога_с_вводом_работает(app: App, tg: Harness) -> None:
    await _open_capacity_input(tg)
    await tg.feed(command_update(OWNER_ID, "/start"))

    texts = [d["text"] for d in tg.session.of("SendMessage")]
    assert any("Я помогаю записываться на встречи" in text for text in texts)
    assert app.services.settings.get_now("capacity") == 10


async def test_help_внутри_диалога_с_вводом_работает(app: App, tg: Harness) -> None:
    await _open_capacity_input(tg)
    await tg.feed(command_update(OWNER_ID, "/help"))

    texts = [d["text"] for d in tg.session.of("SendMessage")]
    assert any("Как пользоваться ботом" in text for text in texts)


async def test_справка_админа_без_устаревших_обещаний(app: App, tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/help"))
    text = tg.session.of("SendMessage")[-1]["text"]
    assert "/menu" in text
    assert "следующих версий" not in text
