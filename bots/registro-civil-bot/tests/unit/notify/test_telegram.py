"""TelegramNotifier (SPEC 2.4): сообщение владельцу, повторы, кнопки, ошибки Bot API."""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe, SendMessage

from rcbot.domain.models import Alert, AlertAction, AlertKind, Channel
from rcbot.infrastructure.notify.telegram import (
    CALLBACK_ACK,
    CALLBACK_MANUAL_DONE,
    MAX_TEXT,
    REQUEST_TIMEOUT_SEC,
    TelegramNotifier,
    keyboard,
)
from tests.unit.bot.harness import OWNER, ApiFailure, FakeSession, make_bot
from tests.unit.notify.helpers import all_alerts


def make() -> tuple[TelegramNotifier, FakeSession]:
    bot, session = make_bot()
    return TelegramNotifier(bot, OWNER), session


def alert(text: str = "🟢 места", action: AlertAction = AlertAction.NONE, **kw: object) -> Alert:
    return Alert(AlertKind.APPEARED, text, action=action, **kw)  # type: ignore[arg-type]


def buttons(message: SendMessage) -> list[tuple[str, str | None]]:
    markup = message.reply_markup
    assert markup is not None and hasattr(markup, "inline_keyboard")
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


# --- отправка ---


async def test_сообщение_уходит_владельцу_с_нужными_параметрами() -> None:
    channel, session = make()
    result = await channel.send(alert("Привет, <b>мир</b> & co", incident_id=7), 1)
    assert result.ok and result.channel is Channel.TELEGRAM and result.error == ""
    assert result.attempt == 1 and result.incident_id == 7
    (message,) = session.sent
    assert message.chat_id == OWNER
    assert message.text == "Привет, <b>мир</b> & co"  # без разметки: текст не искажается
    assert message.disable_web_page_preview is True
    assert session.calls[0].timeout == REQUEST_TIMEOUT_SEC == 10


async def test_настоящий_ответ_с_сообщением_разбирается_как_в_боевой_сессии() -> None:
    """Сессия отдаёт JSON через `check_response`, а не готовые объекты: десериализация настоящая."""
    channel, session = make()
    await channel.send(alert(), 1)
    assert type(session).__name__ == "FakeSession" and session.closed is False


async def test_первая_отправка_без_префикса_повтор_с_префиксом() -> None:
    channel, session = make()
    for attempt in (1, 2, 5):
        await channel.send(alert("текст"), attempt)
    assert session.sent_texts == ["текст", "🔁 Повтор 2\nтекст", "🔁 Повтор 5\nтекст"]


@pytest.mark.parametrize("attempt", [0, 1])
async def test_префикса_нет_при_попытке_не_больше_первой(attempt: int) -> None:
    channel, session = make()
    await channel.send(alert("текст"), attempt)
    assert session.sent_texts == ["текст"]


async def test_длинный_текст_усекается_до_4096_включая_префикс() -> None:
    channel, session = make()
    await channel.send(alert("я" * 6000), 1)
    await channel.send(alert("ж" * 6000), 4)
    first, second = session.sent_texts
    assert first == "я" * MAX_TEXT == "я" * 4096
    assert len(second) == MAX_TEXT and second.startswith("🔁 Повтор 4\n")


async def test_текст_ровно_4096_не_меняется() -> None:
    channel, session = make()
    await channel.send(alert("а" * 4096), 1)
    assert session.sent_texts == ["а" * 4096]


@pytest.mark.parametrize("name", list(all_alerts()))
async def test_каждый_вид_тревоги_помещается_в_лимит_и_отправляется(name: str) -> None:
    channel, session = make()
    result = await channel.send(all_alerts()[name], 2)
    assert result.ok and len(session.sent_texts[0]) <= MAX_TEXT


# --- клавиатура ---


async def test_кнопка_вижу_для_ack() -> None:
    channel, session = make()
    await channel.send(alert(action=AlertAction.ACK), 1)
    assert buttons(session.sent[0]) == [("✅ Вижу", "ack")]
    assert CALLBACK_ACK == "ack"


async def test_кнопка_готово_для_manual_done() -> None:
    channel, session = make()
    await channel.send(alert(action=AlertAction.MANUAL_DONE), 1)
    ((text, data),) = buttons(session.sent[0])
    assert "Готово" in text and data == CALLBACK_MANUAL_DONE == "manual_done"


async def test_без_кнопки_для_none() -> None:
    channel, session = make()
    await channel.send(alert(action=AlertAction.NONE), 1)
    assert session.sent[0].reply_markup is None


def test_данные_кнопок_укладываются_в_лимит_telegram_64_байта() -> None:
    for action in (AlertAction.ACK, AlertAction.MANUAL_DONE):
        markup = keyboard(action)
        assert markup is not None
        for row in markup.inline_keyboard:
            for button in row:
                assert button.callback_data and len(button.callback_data.encode()) <= 64


def test_для_none_клавиатуры_нет() -> None:
    assert keyboard(AlertAction.NONE) is None


async def test_кнопка_есть_и_у_повтора() -> None:
    """Повтор тревоги тоже можно подтвердить — иначе повторы нечем остановить."""
    channel, session = make()
    await channel.send(alert(action=AlertAction.ACK), 3)
    assert buttons(session.sent[0]) == [("✅ Вижу", "ack")]


# --- ошибки ---


async def test_flood_wait_возвращает_ошибку_с_числом_секунд() -> None:
    channel, session = make()
    session.script(SendMessage, ApiFailure(429, "Too Many Requests: retry after 7", retry_after=7))
    result = await channel.send(alert(), 1)
    assert not result.ok
    assert result.error == "Telegram просит подождать 7 с"
    assert result.channel is Channel.TELEGRAM and result.latency_ms >= 0


@pytest.mark.parametrize(
    ("failure", "name"),
    [
        (ApiFailure(400, "Bad Request: message is too long"), "TelegramBadRequest"),
        (ApiFailure(403, "Forbidden: bot was blocked by the user"), "TelegramForbiddenError"),
        (ApiFailure(401, "Unauthorized"), "TelegramUnauthorizedError"),
        (ApiFailure(502, "Bad Gateway"), "TelegramServerError"),
        (ApiFailure(418, "я чайник"), "TelegramAPIError"),
    ],
)
async def test_ошибка_bot_api_это_ошибка_результата(failure: ApiFailure, name: str) -> None:
    channel, session = make()
    session.script(SendMessage, failure)
    result = await channel.send(alert(), 2)
    assert not result.ok and result.error == name and result.attempt == 2


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (OSError("сеть"), "OSError"),
        (ConnectionResetError(), "ConnectionResetError"),
        (TimeoutError(), "TimeoutError"),
        (TelegramNetworkError(SendMessage(chat_id=1, text="т"), "обрыв"), "TelegramNetworkError"),
    ],
)
async def test_сетевая_ошибка_это_ошибка_результата_без_исключения(
    error: BaseException, name: str
) -> None:
    channel, session = make()
    session.script(SendMessage, error)
    result = await channel.send(alert(), 1)
    assert not result.ok and result.error == name


async def test_после_сбоя_канал_работает_дальше() -> None:
    channel, session = make()
    session.script(SendMessage, OSError("раз"))
    first = await channel.send(alert("одно"), 1)
    second = await channel.send(alert("два"), 2)
    assert (first.ok, second.ok) == (False, True)


async def test_ошибка_не_раскрывает_текст_тревоги() -> None:
    channel, session = make()
    session.script(SendMessage, ApiFailure(400, "Bad Request: SECRET-TEXT-IN-DESCRIPTION"))
    result = await channel.send(alert("номер TEST-TRAMITE-1"), 1)
    assert "SECRET" not in result.error and "TEST-TRAMITE" not in result.error


async def test_не_ошибка_telegram_не_маскируется() -> None:
    channel, session = make()
    session.script(SendMessage, ValueError("баг программиста"))
    with pytest.raises(ValueError):
        await channel.send(alert(), 1)


# --- прогрев и закрытие ---


async def test_прогрев_это_getme_с_таймаутом() -> None:
    channel, session = make()
    await channel.warmup()
    assert [type(c.method) for c in session.calls] == [GetMe]
    assert session.calls[0].timeout == REQUEST_TIMEOUT_SEC


async def test_сбой_прогрева_пробрасывается_наверх() -> None:
    """Рассылка ловит его сама (alerting.keep_warm); канал ошибку не прячет."""
    channel, session = make()
    session.script(GetMe, OSError("нет сети"))
    with pytest.raises(OSError):
        await channel.warmup()


async def test_close_не_закрывает_общую_сессию_бота() -> None:
    channel, session = make()
    await channel.close()
    assert session.closed is False


def test_канал_это_telegram() -> None:
    assert make()[0].channel is Channel.TELEGRAM
