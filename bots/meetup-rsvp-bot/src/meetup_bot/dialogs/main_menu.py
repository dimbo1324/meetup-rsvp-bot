"""Главное меню админа: команды /menu и /cancel и инструкция по рассылке поста."""

from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.kbd import Button, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.dialogs._buttons import TO_MENU
from meetup_bot.dialogs._common import guard
from meetup_bot.dialogs._rights import guard_owner, is_owner
from meetup_bot.dialogs.states import AdminsSG, BansSG, EventsSG, MainSG, SettingsSG
from meetup_bot.telegram.errors_text import NO_ACCESS

router = Router(name="dialogs_main_menu")
router.message.filter(F.chat.type == "private")


async def _admin_only(message: Message, manager: DialogManager) -> bool:
    if message.from_user is None:
        await message.answer(NO_ACCESS)
        return False
    return await guard(message, manager, close=False)


@router.message(Command("menu"))
async def cmd_menu(message: Message, dialog_manager: DialogManager) -> None:
    if not await _admin_only(message, dialog_manager):
        return
    # RESET_STACK: зависший диалог не должен оставаться под новым меню
    await dialog_manager.start(MainSG.menu, mode=StartMode.RESET_STACK)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, dialog_manager: DialogManager) -> None:
    if not await _admin_only(message, dialog_manager):
        return
    await dialog_manager.reset_stack()
    # Если был открыт выбор пользователя, reply-клавиатуру при удалении окна не снять
    await message.answer("Диалог закрыт. Меню — команда /menu.", reply_markup=ReplyKeyboardRemove())


async def _menu_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    return {"is_owner": await is_owner(dialog_manager)}


async def _howto_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    username = dialog_manager.middleware_data.get("bot_username", "")
    return {"bot": html.escape(str(username), quote=False)}


def _opener(state: Any, *, owner_only: bool = False) -> Any:
    """Кнопка запускает диалог после повторной проверки прав (скрытая кнопка — не защита)."""

    async def on_click(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
        if owner_only:
            allowed = await guard_owner(callback, manager)
        else:
            allowed = await guard(callback, manager, close=False)
        if allowed:
            await manager.start(state)

    return on_click


HOWTO_TEXT = (
    "<b>Как разослать пост</b>\n\n"
    "1. Откройте нужный чат и наберите <code>@{bot}</code> (с пробелом после имени).\n"
    "2. В появившемся списке выберите встречу и нажмите на неё.\n"
    "3. Пост отправится в чат от вашего имени с пометкой «через @{bot}».\n\n"
    "Постов у одной встречи может быть сколько угодно, в разных чатах: счётчик участников "
    "общий у всех постов и обновляется сам."
)

DIALOGS = [
    Dialog(
        Window(
            Const("<b>Меню администратора</b>"),
            Button(Const("📅 Встречи"), id="events", on_click=_opener(EventsSG.list)),
            Button(Const("🚫 Чёрный список"), id="bans", on_click=_opener(BansSG.list)),
            Button(Const("⚙️ Настройки"), id="settings", on_click=_opener(SettingsSG.menu)),
            Button(
                Const("👑 Админы"),
                id="admins",
                on_click=_opener(AdminsSG.list, owner_only=True),
                when="is_owner",
            ),
            SwitchTo(Const("ℹ️ Как разослать пост"), id="howto", state=MainSG.howto),
            state=MainSG.menu,
            getter=_menu_getter,
        ),
        Window(
            Format(HOWTO_TEXT),
            SwitchTo(Const(TO_MENU), id="to_menu", state=MainSG.menu),
            state=MainSG.howto,
            getter=_howto_getter,
        ),
    )
]
