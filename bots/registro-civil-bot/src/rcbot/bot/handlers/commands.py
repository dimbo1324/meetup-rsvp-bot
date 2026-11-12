"""Команды SPEC 2.5: /status /pause /resume /test /last /interval /relogin /cleanup /menu."""

from __future__ import annotations

import math
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from aiogram_dialog import DialogManager, StartMode

from rcbot.application.control import MAX_PAUSE_MINUTES, Controller, IntervalTooSmall
from rcbot.bot import texts
from rcbot.bot.dialogs.states import MenuSG

# Дольше года пауза «на минуты» не бывает; для паузы без срока есть /pause без числа
PAUSE_BAD = "Нужно число минут от 1 до 525600: /pause 30 (или просто /pause)"


def _pause_minutes(args: str) -> int | None:
    """Минуты из аргумента или `None`, если это не целое число от 1 до года."""
    raw = args.strip()
    # str.isdigit() пропускает «²» и арабские цифры, на которых int() падает или даёт неожиданное
    if not (raw.isascii() and raw.isdigit()):
        return None
    minutes = int(raw)
    return minutes if 1 <= minutes <= MAX_PAUSE_MINUTES else None


async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


async def cmd_status(message: Message, controller: Controller, tz: ZoneInfo) -> None:
    await message.answer(texts.status_text(await controller.status(), tz))


async def cmd_pause(
    message: Message, command: CommandObject, controller: Controller, tz: ZoneInfo
) -> None:
    minutes: int | None = None
    if command.args and command.args.strip():
        minutes = _pause_minutes(command.args)
        if minutes is None:
            await message.answer(PAUSE_BAD)
            return
    until = await controller.pause(minutes)
    if minutes:
        await message.answer(texts.PAUSED.format(until=f"{until.astimezone(tz):%d.%m %H:%M}"))
    else:
        await message.answer(texts.PAUSED_FOREVER)


async def cmd_resume(message: Message, controller: Controller) -> None:
    await controller.resume()
    await message.answer(texts.RESUMED)


async def cmd_interval(message: Message, command: CommandObject, controller: Controller) -> None:
    raw = (command.args or "").strip().replace(",", ".")
    try:
        seconds = float(raw)
        # float() принимает «nan» и «inf»: nan проходит любые сравнения и ломает расписание
        if not math.isfinite(seconds):
            raise ValueError(raw)
        value = await controller.set_interval(seconds)
    except ValueError, IntervalTooSmall:
        await message.answer(texts.INTERVAL_BAD)
        return
    await message.answer(texts.INTERVAL_SET.format(value=value))


async def cmd_test(message: Message, controller: Controller) -> None:
    await message.answer("Отправляю тест во все каналы…")
    await message.answer(texts.deliveries_text(await controller.test()))


async def cmd_last(message: Message, controller: Controller, tz: ZoneInfo) -> None:
    await message.answer(texts.last_text(await controller.last(5), tz))


async def cmd_relogin(message: Message, controller: Controller) -> None:
    await message.answer("Вхожу заново…")
    error = await controller.relogin()
    await message.answer(texts.RELOGIN_FAILED.format(error=error) if error else texts.RELOGIN_OK)


async def cmd_cleanup(message: Message, controller: Controller) -> None:
    await message.answer(texts.cleanup_text(await controller.cleanup()))


async def cmd_menu(message: Message, dialog_manager: DialogManager) -> None:
    await dialog_manager.start(MenuSG.main, mode=StartMode.RESET_STACK)


def build_router() -> Router:
    """Новый роутер на каждый Dispatcher: один роутер нельзя подключить к двум."""
    router = Router(name="commands")
    router.message.register(cmd_help, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_status, Command("status"))
    router.message.register(cmd_pause, Command("pause"))
    router.message.register(cmd_resume, Command("resume"))
    router.message.register(cmd_interval, Command("interval"))
    router.message.register(cmd_test, Command("test"))
    router.message.register(cmd_last, Command("last"))
    router.message.register(cmd_relogin, Command("relogin"))
    router.message.register(cmd_cleanup, Command("cleanup"))
    router.message.register(cmd_menu, Command("menu"))
    return router
