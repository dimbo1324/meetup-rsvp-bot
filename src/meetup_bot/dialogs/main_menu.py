"""Главное меню админа: команды /menu и /cancel и инструкция по рассылке поста."""

from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, StartMode, Window
from aiogram_dialog.widgets.kbd import Button, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.dialogs._common import NO_ACCESS, actor_id, get_sessions, is_admin
from meetup_bot.dialogs.states import AdminsSG, BansSG, EventsSG, MainSG, SettingsSG
from meetup_bot.handlers._common import check_admin
from meetup_bot.repositories import admins

router = Router(name="dialogs_main_menu")
router.message.filter(F.chat.type == "private")


@router.message(Command("menu"))
async def cmd_menu(message: Message, dialog_manager: DialogManager) -> None:
    if message.from_user is None or not await check_admin(
        get_sessions(dialog_manager), message.from_user.id
    ):
        await message.answer(NO_ACCESS)
        return
    # RESET_STACK: зависший диалог не должен оставаться под новым меню
    await dialog_manager.start(MainSG.menu, mode=StartMode.RESET_STACK)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, dialog_manager: DialogManager) -> None:
    if message.from_user is None or not await check_admin(
        get_sessions(dialog_manager), message.from_user.id
    ):
        await message.answer(NO_ACCESS)
        return
    await dialog_manager.reset_stack()
    await message.answer("Диалог закрыт. Меню — команда /menu.")


async def _menu_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    async with get_sessions(dialog_manager)() as session:
        owner = await admins.is_owner(session, actor_id(dialog_manager))
    return {"is_owner": owner}


async def _howto_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    username = dialog_manager.middleware_data.get("bot_username", "")
    return {"bot": html.escape(str(username), quote=False)}


def _opener(state: Any, *, owner_only: bool = False) -> Any:
    """Кнопка запускает диалог после повторной проверки прав (скрытая кнопка — не защита)."""

    async def on_click(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
        allowed = await is_admin(manager)
        if allowed and owner_only:
            async with get_sessions(manager)() as session:
                allowed = await admins.is_owner(session, actor_id(manager))
        if not allowed:
            await callback.answer(NO_ACCESS, show_alert=True)
            return
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
            SwitchTo(Const("⬅️ В меню"), id="to_menu", state=MainSG.menu),
            state=MainSG.howto,
            getter=_howto_getter,
        ),
    )
]
