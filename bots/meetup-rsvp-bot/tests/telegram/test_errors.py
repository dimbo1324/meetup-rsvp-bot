"""Глобальный обработчик ошибок: ответ на callback, троттлинг отчётов, устаревшие диалоги."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from aiogram_dialog.api.exceptions import (
    InvalidStackIdError,
    OutdatedIntent,
    UnknownIntent,
    UnknownState,
)

from meetup_bot.handlers import errors
from meetup_bot.handlers.errors import REPORT_INTERVAL, STALE_DIALOG_TEXT
from meetup_bot.telegram.common import GENERIC_ERROR_TEXT
from tests.helpers import App
from tests.tg_fake import Harness, callback_update, command_update

USER = 5001
SERVICE_CHAT = -100500


@pytest.fixture(autouse=True)
def чистые_отчёты() -> Iterator[None]:
    """Журнал последних отчётов — глобальный; изолируем тесты друг от друга."""
    errors._last_report.clear()
    yield
    errors._last_report.clear()


class BrokenError(RuntimeError):
    pass


def reports(tg: Harness) -> list[str]:
    """Сообщения, ушедшие в служебный чат."""
    return [c["text"] for c in tg.session.of("SendMessage") if c["chat_id"] == SERVICE_CHAT]


def break_start(monkeypatch: pytest.MonkeyPatch, app: App, error: Exception) -> None:
    """Ломает /start так, чтобы исключение вышло за пределы обработчика."""

    async def boom(*_a: Any, **_k: Any) -> None:
        raise error

    monkeypatch.setattr(app.services.users, "is_admin", boom)


async def test_ошибка_в_команде_уходит_в_служебный_чат(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    break_start(monkeypatch, app, BrokenError("секрет"))
    await tg.feed(command_update(USER, "/start"))
    assert reports(tg) == ["⚠️ Ошибка: BrokenError"]


async def test_отчёт_содержит_только_тип_ошибки(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    break_start(monkeypatch, app, BrokenError("секретные данные пользователя"))
    await tg.feed(command_update(USER, "/start"))
    assert "секретные" not in reports(tg)[0]


async def test_ошибка_в_callback_получает_общий_ответ(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise BrokenError

    # Падает сама запись «устаревшей кнопки»: исключение выходит из обработчика
    monkeypatch.setattr("meetup_bot.handlers.signup.safe_answer", boom)
    await tg.feed(callback_update(USER, "r:abc"))
    (answer,) = tg.session.of("AnswerCallbackQuery")
    assert answer["text"] == GENERIC_ERROR_TEXT
    assert answer["show_alert"] is True


async def test_повторная_ошибка_того_же_типа_в_течение_минуты_не_дублируется(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    break_start(monkeypatch, app, BrokenError())
    await tg.feed(command_update(USER, "/start"))
    app.clock.advance(timedelta(seconds=30))
    await tg.feed(command_update(USER, "/start"))
    assert len(reports(tg)) == 1


async def test_через_минуту_отчёт_отправляется_снова(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    break_start(monkeypatch, app, BrokenError())
    await tg.feed(command_update(USER, "/start"))
    app.clock.advance(REPORT_INTERVAL)
    await tg.feed(command_update(USER, "/start"))
    assert len(reports(tg)) == 2


async def test_ошибки_разных_типов_троттлятся_независимо(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    break_start(monkeypatch, app, BrokenError())
    await tg.feed(command_update(USER, "/start"))
    break_start(monkeypatch, app, KeyError("k"))
    await tg.feed(command_update(USER, "/start"))
    assert reports(tg) == ["⚠️ Ошибка: BrokenError", "⚠️ Ошибка: KeyError"]


async def test_сбой_отправки_отчёта_не_ломает_обработчик(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_send(_text: str) -> None:
        raise ConnectionError

    break_start(monkeypatch, app, BrokenError())
    monkeypatch.setattr(tg.notifier, "send_service", fail_send)
    await tg.feed(command_update(USER, "/start"))


async def test_ответ_на_callback_не_ломает_обработчик_при_сбое_ответа(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise BrokenError

    monkeypatch.setattr("meetup_bot.handlers.signup.safe_answer", boom)
    tg.session.fail = lambda name, _d: (
        ConnectionError("нет сети") if name == "AnswerCallbackQuery" else None
    )
    await tg.feed(callback_update(USER, "r:abc"))
    assert len(tg.session.of("AnswerCallbackQuery")) == 1


@pytest.mark.parametrize(
    "error",
    [
        UnknownIntent("нет интента"),
        OutdatedIntent("a", "b"),
        UnknownState("нет состояния"),
        InvalidStackIdError("стека нет"),
    ],
    ids=["UnknownIntent", "OutdatedIntent", "UnknownState", "InvalidStackIdError"],
)
async def test_устаревший_диалог_отвечает_подсказкой_без_отчёта(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise error

    monkeypatch.setattr("meetup_bot.handlers.signup.safe_answer", boom)
    await tg.feed(callback_update(USER, "r:abc"))
    (answer,) = tg.session.of("AnswerCallbackQuery")
    assert answer["text"] == STALE_DIALOG_TEXT
    assert answer["show_alert"] is True
    assert reports(tg) == []


async def test_устаревший_диалог_в_сообщении_отвечает_подсказкой_и_снимает_клавиатуру(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    break_start(monkeypatch, app, UnknownIntent("нет интента"))
    await tg.feed(command_update(USER, "/start"))
    assert tg.session.of("AnswerCallbackQuery") == []
    assert reports(tg) == []
    (reply,) = [c for c in tg.session.of("SendMessage") if c["chat_id"] == USER]
    assert reply["text"] == STALE_DIALOG_TEXT
    assert reply["reply_markup"] == {"remove_keyboard": True}


async def test_сбой_ответа_на_устаревшую_кнопку_игнорируется(
    app: App, tg: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise UnknownIntent("нет интента")

    monkeypatch.setattr("meetup_bot.handlers.signup.safe_answer", boom)
    tg.session.fail = lambda name, _d: (
        ConnectionError("нет сети") if name == "AnswerCallbackQuery" else None
    )
    await tg.feed(callback_update(USER, "r:abc"))
    assert reports(tg) == []
