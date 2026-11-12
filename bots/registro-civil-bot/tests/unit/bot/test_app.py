"""Сборка Dispatcher (SPEC 2.5): фильтр владельца снаружи, список команд, long polling."""

from __future__ import annotations

import asyncio
import re

from aiogram.methods import AnswerCallbackQuery, GetUpdates, SetMyCommands
from aiogram.types import CallbackQuery, ErrorEvent, Update, User
from aiogram_dialog.api.exceptions import UnknownIntent

from rcbot.bot.app import ALLOWED_UPDATES, COMMANDS, _on_stale_dialog, build_dispatcher, run_polling
from rcbot.bot.middlewares import OwnerOnlyMiddleware
from tests.unit.bot.harness import (
    OWNER,
    TZ,
    ApiFailure,
    make_bot,
    make_rig,
    make_setup,
    settled,
)

SPEC_COMMANDS = [
    "status",
    "pause",
    "resume",
    "test",
    "last",
    "interval",
    "relogin",
    "cleanup",
    "menu",
]


async def test_dispatcher_можно_собрать_несколько_раз_в_одном_процессе() -> None:
    """Регрессия: роутеры были общими на модуль и второй `build_dispatcher` падал."""
    rig = await make_rig()
    first = build_dispatcher(rig.controller, TZ, OWNER)
    second = build_dispatcher(rig.controller, TZ, OWNER)
    assert first is not second


async def test_два_dispatcher_работают_независимо() -> None:
    one, two = await make_setup(), await make_setup()
    await one.owner.say("/status")
    await two.owner.say("/help")
    assert len(one.session.sent) == 1 and len(two.session.sent) == 1


def test_список_команд_соответствует_spec_2_5() -> None:
    assert [c.command for c in COMMANDS] == SPEC_COMMANDS


def test_команды_подходят_под_правила_telegram() -> None:
    for command in COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", command.command)
        assert 3 <= len(command.description) <= 256


def test_allowed_updates_только_сообщения_и_нажатия() -> None:
    assert sorted(ALLOWED_UPDATES) == ["callback_query", "message"]


async def test_фильтр_владельца_стоит_снаружи_для_сообщений_и_нажатий() -> None:
    rig = await make_rig()
    dp = build_dispatcher(rig.controller, TZ, OWNER)
    for observer in (dp.message, dp.callback_query):
        owners = [m for m in observer.outer_middleware if isinstance(m, OwnerOnlyMiddleware)]
        assert len(owners) == 1


async def test_данные_для_обработчиков_контроллер_и_часовой_пояс() -> None:
    rig = await make_rig()
    dp = build_dispatcher(rig.controller, TZ, OWNER)
    assert dp.workflow_data["controller"] is rig.controller
    assert dp.workflow_data["tz"] is TZ


async def test_run_polling_ставит_команды_и_слушает_только_нужные_обновления() -> None:
    rig = await make_rig()
    dp = build_dispatcher(rig.controller, TZ, OWNER)
    bot, session = make_bot()
    task = asyncio.create_task(run_polling(bot, dp))
    for _ in range(50):
        await settled()
        if session.of(GetUpdates):
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    (commands,) = session.of(SetMyCommands)
    assert [c.command for c in commands.commands] == SPEC_COMMANDS
    assert session.of(GetUpdates)[0].allowed_updates == ALLOWED_UPDATES


def _stale_event(bot, with_callback: bool) -> ErrorEvent:  # type: ignore[no-untyped-def]
    callback = CallbackQuery(
        id="old", from_user=User(id=OWNER, is_bot=False, first_name="Т"), chat_instance="x"
    ).as_(bot)
    update = Update(update_id=1, callback_query=callback if with_callback else None)
    return ErrorEvent(update=update, exception=UnknownIntent("нет контекста"))


async def test_устаревшая_кнопка_получает_ответ() -> None:
    bot, session = make_bot()
    await _on_stale_dialog(_stale_event(bot, True))
    assert [a.text for a in session.answers] == ["Меню устарело: откройте заново /menu"]


async def test_устаревшее_событие_без_нажатия_молча_пропускается() -> None:
    bot, session = make_bot()
    await _on_stale_dialog(_stale_event(bot, False))
    assert session.calls == []


async def test_слишком_старое_нажатие_не_роняет_обработчик_ошибок() -> None:
    """Telegram отвечает 400 «query is too old»: отвечать уже некуда, это не авария."""
    bot, session = make_bot()
    session.script(AnswerCallbackQuery, ApiFailure(400, "Bad Request: query is too old"))
    await _on_stale_dialog(_stale_event(bot, True))
