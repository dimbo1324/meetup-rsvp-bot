"""UserMiddleware: актуализация данных пользователя при каждом взаимодействии."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.types import ChosenInlineResult, InlineQuery, Message, Update
from aiogram.types import User as TgUser

from meetup_bot.db.models import User
from meetup_bot.middlewares.user import UserMiddleware
from meetup_bot.repositories.users import UserInfo
from tests.helpers import App
from tests.tg_fake import Harness, callback_update, chat, command_update, next_id, tg_user

USER = 6001


async def get_user(app: App, user_id: int) -> User | None:
    async with app.sessions() as session:
        return await session.get(User, user_id)


def message_from(user: TgUser, text: str = "/help") -> Update:
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=datetime.now(UTC),
            chat=chat(user.id),
            from_user=user,
            text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text)}],
        ),
    )


class Probe:
    """Обработчик-пустышка: запоминает, что дошло до него."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, event: Any, data: dict[str, Any]) -> str:
        self.calls.append(data)
        return "handled"


class BrokenSessions:
    def __call__(self) -> Any:
        raise RuntimeError("БД недоступна")


def make_data(app: App, user: TgUser | None, sessions: Any = None) -> dict[str, Any]:
    return {
        "event_from_user": user,
        "sessions": sessions or app.sessions,
        "clock": app.clock,
    }


# ---- через диспетчер ----


async def test_сообщение_создаёт_пользователя_в_базе(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/help"))
    user = await get_user(app, USER)
    assert user is not None
    assert (user.first_name, user.username) == ("Люди6001", "u6001")
    assert user.dm_available is False
    assert user.created_at == app.clock.now()


async def test_нажатие_кнопки_создаёт_пользователя(app: App, tg: Harness) -> None:
    await tg.feed(callback_update(USER, "r:abc"))
    assert await get_user(app, USER) is not None


async def test_смена_имени_и_username_доходит_до_базы(app: App, tg: Harness) -> None:
    await tg.feed(command_update(USER, "/help"))
    app.clock.advance(timedelta(hours=1))
    renamed = TgUser(
        id=USER, is_bot=False, first_name="Новое", last_name="Имя", username="new_nick"
    )
    await tg.feed(message_from(renamed))
    user = await get_user(app, USER)
    assert user is not None
    assert (user.first_name, user.last_name, user.username) == ("Новое", "Имя", "new_nick")
    assert user.updated_at == app.clock.now()


async def test_бота_в_базу_не_пишем_и_до_обработчика_не_пускаем(app: App, tg: Harness) -> None:
    bot_user = TgUser(id=7007, is_bot=True, first_name="Другой бот")
    await tg.feed(message_from(bot_user, "/start"))
    assert await get_user(app, 7007) is None
    assert tg.session.calls == []


async def test_inline_запрос_не_пишет_в_базу(app: App, tg: Harness) -> None:
    update = Update(
        update_id=next_id(),
        inline_query=InlineQuery(id="1", from_user=tg_user(USER), query="", offset=""),
    )
    await tg.feed(update)
    assert await get_user(app, USER) is None


async def test_выбранный_inline_результат_не_пишет_в_базу(app: App, tg: Harness) -> None:
    update = Update(
        update_id=next_id(),
        chosen_inline_result=ChosenInlineResult(
            result_id="x", from_user=tg_user(USER), query="", inline_message_id="m"
        ),
    )
    await tg.feed(update)
    assert await get_user(app, USER) is None


# ---- напрямую ----


async def test_user_info_передаётся_обработчику(app: App) -> None:
    probe = Probe()
    user = TgUser(id=USER, is_bot=False, first_name="Анна", last_name="Ли", username="anna")
    result = await UserMiddleware()(probe, command_update(USER, "/x").message, make_data(app, user))  # type: ignore[arg-type]
    assert result == "handled"
    assert probe.calls[0]["user_info"] == UserInfo(USER, "Анна", "Ли", "anna")


async def test_без_пользователя_обработчик_не_вызывается(app: App) -> None:
    probe = Probe()
    result = await UserMiddleware()(probe, command_update(USER, "/x").message, make_data(app, None))  # type: ignore[arg-type]
    assert result is None
    assert probe.calls == []


async def test_сбой_записи_в_базу_не_блокирует_обработку(
    app: App, caplog: pytest.LogCaptureFixture
) -> None:
    probe = Probe()
    user = tg_user(USER)
    data = make_data(app, user, BrokenSessions())
    result = await UserMiddleware()(probe, command_update(USER, "/x").message, data)  # type: ignore[arg-type]
    assert result == "handled"
    assert probe.calls[0]["user_info"].id == USER
    assert "не удалось обновить данные пользователя" in caplog.text


async def test_inline_запрос_напрямую_не_обращается_к_базе(app: App) -> None:
    probe = Probe()
    query = InlineQuery(id="1", from_user=tg_user(USER), query="", offset="")
    data = make_data(app, tg_user(USER), BrokenSessions())
    assert await UserMiddleware()(probe, query, data) == "handled"
    assert probe.calls[0]["user_info"].id == USER
