"""OwnerOnlyMiddleware (SPEC 2.4–2.6, инвариант 10): бот отвечает только владельцу в его личке."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Chat, InlineQuery, Message, Update, User
from hypothesis import given
from hypothesis import strategies as st

from rcbot.bot.middlewares import OwnerOnlyMiddleware
from tests.unit.bot.harness import GROUP, OWNER, STRANGER

SENTINEL = object()


class Probe:
    """Обработчик-свидетель: запоминает вызовы."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def __call__(self, event: Any, data: dict[str, Any]) -> Any:
        self.calls.append((event, data))
        return SENTINEL


def message(user_id: int | None, chat_id: int, chat_type: str = "private") -> Message:
    return Message(
        message_id=1,
        date=1_700_000_000,  # type: ignore[arg-type]
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="Т") if user_id is not None else None,
        text="/status",
    )


def callback(user_id: int, chat_id: int | None, chat_type: str = "private") -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=user_id, is_bot=False, first_name="Т"),
        chat_instance="x",
        message=message(user_id, chat_id, chat_type) if chat_id is not None else None,
        data="ack",
    )


async def run(event: Any, owner: int = OWNER) -> tuple[Any, Probe]:
    probe = Probe()
    result = await OwnerOnlyMiddleware(owner)(probe, event, {"k": "v"})
    return result, probe


# --- пропускает владельца ---


async def test_сообщение_владельца_в_личке_проходит() -> None:
    event = message(OWNER, OWNER)
    result, probe = await run(event)
    assert result is SENTINEL
    assert probe.calls == [(event, {"k": "v"})]  # событие и данные переданы дальше без изменений


async def test_нажатие_кнопки_владельца_в_личке_проходит() -> None:
    result, probe = await run(callback(OWNER, OWNER))
    assert result is SENTINEL and len(probe.calls) == 1


async def test_нажатие_владельца_без_сообщения_проходит() -> None:
    """У кнопки из inline-сообщения нет `message`: чат считается личкой самого пользователя."""
    result, probe = await run(callback(OWNER, None))
    assert result is SENTINEL and len(probe.calls) == 1


# --- не пропускает остальных, молча ---


async def test_чужое_сообщение_не_проходит_и_без_ответа() -> None:
    result, probe = await run(message(STRANGER, STRANGER))
    assert result is None and probe.calls == []


async def test_чужое_нажатие_не_проходит() -> None:
    result, probe = await run(callback(STRANGER, STRANGER))
    assert result is None and probe.calls == []


async def test_чужое_нажатие_на_сообщение_в_чате_владельца_не_проходит() -> None:
    """Кнопка, пересланная чужому: user чужой, хотя сообщение лежит в чате владельца."""
    result, probe = await run(callback(STRANGER, OWNER))
    assert result is None and probe.calls == []


async def test_чужое_сообщение_с_чатом_владельца_не_проходит() -> None:
    result, probe = await run(message(STRANGER, OWNER))
    assert result is None and probe.calls == []


async def test_владелец_в_группе_не_проходит() -> None:
    result, probe = await run(message(OWNER, GROUP, "supergroup"))
    assert result is None and probe.calls == []


async def test_нажатие_владельца_в_группе_не_проходит() -> None:
    result, probe = await run(callback(OWNER, GROUP, "supergroup"))
    assert result is None and probe.calls == []


async def test_сообщение_без_отправителя_не_проходит() -> None:
    """Пост канала или анонимный админ группы: `from_user` пуст."""
    result, probe = await run(message(None, OWNER))
    assert result is None and probe.calls == []


async def test_событие_другого_типа_не_проходит() -> None:
    inline = InlineQuery(
        id="1", from_user=User(id=OWNER, is_bot=False, first_name="Т"), query="", offset=""
    )
    for event in (inline, Update(update_id=1)):
        result, probe = await run(event)
        assert result is None and probe.calls == []


async def test_исключение_обработчика_не_гасится() -> None:
    async def boom(event: Any, data: dict[str, Any]) -> Any:
        raise RuntimeError("сбой обработчика")

    with pytest.raises(RuntimeError):
        await OwnerOnlyMiddleware(OWNER)(boom, message(OWNER, OWNER), {})


@given(user=st.integers(-(2**40), 2**40), chat=st.integers(-(2**40), 2**40))
async def test_свойство_проходит_только_пара_владелец_и_его_чат(user: int, chat: int) -> None:
    result, probe = await run(message(user, chat), owner=OWNER)
    passes = user == OWNER and chat == OWNER
    assert (result is SENTINEL) is passes
    assert bool(probe.calls) is passes


@given(user=st.integers(-(2**40), 2**40), chat=st.integers(-(2**40), 2**40))
async def test_свойство_для_нажатий_то_же_правило(user: int, chat: int) -> None:
    result, _ = await run(callback(user, chat), owner=OWNER)
    passes = user == OWNER and chat == OWNER
    assert (result is SENTINEL) is passes


# --- журнал ---


async def test_в_журнал_идёт_тип_события_а_не_номер_чужого_пользователя(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    await run(message(STRANGER, STRANGER))
    await run(callback(STRANGER, STRANGER))
    ours = [r for r in caplog.records if r.name == "rcbot.bot.middlewares"]
    assert len(ours) == 2, "отброшенное обновление должно попадать в журнал (SPEC 2.4)"
    text = "\n".join(r.getMessage() for r in ours)
    assert "Message" in text and "CallbackQuery" in text
    assert str(STRANGER) not in text
    assert all(r.levelno == logging.INFO for r in ours)


async def test_пропущенное_обновление_не_шумит_в_журнале(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await run(message(OWNER, OWNER))
    assert not [r for r in caplog.records if r.name == "rcbot.bot.middlewares"]
