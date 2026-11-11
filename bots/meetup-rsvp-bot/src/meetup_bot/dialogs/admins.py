"""Управление админами: только для владельца."""

from __future__ import annotations

import html
from typing import Any

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.kbd import Button, Column, Row, ScrollingGroup, Select
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.db.models import AdminRole
from meetup_bot.dialogs._buttons import BACK, LIST_PAGE_SIZE, NO
from meetup_bot.dialogs._common import actor_id, get_bot, get_services, picked_user
from meetup_bot.dialogs._rights import guard_owner, is_owner
from meetup_bot.dialogs.states import AdminsSG, PickerSG
from meetup_bot.services.admins import AdminEntry
from meetup_bot.services.errors import DomainError
from meetup_bot.services.names import admin_label
from meetup_bot.telegram.commands import set_admin_commands
from meetup_bot.telegram.common import safe_answer
from meetup_bot.telegram.errors_text import NO_ACCESS, describe_error

_ROLES = {AdminRole.OWNER.value: "владелец", AdminRole.ADMIN.value: "админ"}
_PICK_TITLE = "Кого сделать админом?"


def _name(entry: AdminEntry) -> str:
    return admin_label(entry.first_name, entry.last_name, entry.username, entry.user_id)


async def _entries(manager: DialogManager) -> list[AdminEntry]:
    return await get_services(manager).admins.list_admins(actor_id(manager))


async def list_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not await is_owner(dialog_manager):
        return {"text": NO_ACCESS, "admins": [], "owner": False}
    try:
        entries = await _entries(dialog_manager)
    except DomainError as exc:
        return {"text": html.escape(describe_error(exc)), "admins": [], "owner": False}
    items = [
        {"id": e.user_id, "label": f"{_name(e)} · {_ROLES.get(e.role, e.role)}"} for e in entries
    ]
    return {"text": f"<b>Админы</b>\nВсего: {len(entries)}.", "admins": items, "owner": True}


async def item_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    user_id = dialog_manager.dialog_data.get("user_id")
    if not await is_owner(dialog_manager) or user_id is None:
        return {"text": NO_ACCESS, "removable": False}
    try:
        entries = await _entries(dialog_manager)
    except DomainError as exc:
        return {"text": html.escape(describe_error(exc)), "removable": False}
    entry = next((e for e in entries if e.user_id == user_id), None)
    if entry is None:
        return {"text": "Админ не найден.", "removable": False}
    text = (
        f"<b>{html.escape(_name(entry))}</b>\nID: {entry.user_id}\n"
        f"Роль: {_ROLES.get(entry.role, entry.role)}"
    )
    return {"text": text, "removable": entry.role != AdminRole.OWNER.value}


async def confirm_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    user_id = dialog_manager.dialog_data.get("user_id")
    if not await is_owner(dialog_manager) or user_id is None:
        return {"text": NO_ACCESS}
    try:
        entries = await _entries(dialog_manager)
    except DomainError as exc:
        return {"text": html.escape(describe_error(exc))}
    entry = next((e for e in entries if e.user_id == user_id), None)
    name = _name(entry) if entry else f"ID {user_id}"
    return {"text": f"Удалить из админов: <b>{html.escape(name)}</b>?"}


async def on_add(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await guard_owner(callback, manager):
        return
    await manager.start(PickerSG.pick, data={"title": _PICK_TITLE, "exclude_ids": []})


async def on_picked(start_data: Any, result: Any, manager: DialogManager) -> None:
    info = picked_user(result)
    if info is None or not await is_owner(manager):
        return
    try:
        await get_services(manager).admins.add_admin(actor_id(manager), info)
    except DomainError as exc:
        text = describe_error(exc)
    else:
        text = "Админ добавлен."
        await set_admin_commands(get_bot(manager), info.id, is_admin=True)
    event = manager.event
    if isinstance(event, CallbackQuery):
        await safe_answer(event, text)
    elif isinstance(event, Message):
        await event.answer(text)


async def on_item(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: int
) -> None:
    if not await guard_owner(callback, manager):
        return
    manager.dialog_data["user_id"] = item_id
    await manager.switch_to(AdminsSG.item)


async def on_remove(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await guard_owner(callback, manager):
        return
    await manager.switch_to(AdminsSG.confirm_remove)


async def on_remove_confirm(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    if not await guard_owner(callback, manager):
        return
    user_id = manager.dialog_data.get("user_id")
    if user_id is None:
        await manager.switch_to(AdminsSG.list)
        return
    try:
        await get_services(manager).admins.remove_admin(actor_id(manager), int(user_id))
    except DomainError as exc:
        await safe_answer(callback, describe_error(exc))
    else:
        await safe_answer(callback, "Админ удалён.", show_alert=False)
        await set_admin_commands(get_bot(manager), int(user_id), is_admin=False)
    manager.dialog_data.pop("user_id", None)
    await manager.switch_to(AdminsSG.list)


async def on_back_to_item(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.switch_to(AdminsSG.item)


async def on_back_to_list(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.switch_to(AdminsSG.list)


async def on_close(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.done()


DIALOGS = [
    Dialog(
        Window(
            Format("{text}"),
            ScrollingGroup(
                Select(
                    Format("{item[label]}"),
                    id="admin_item",
                    item_id_getter=lambda item: item["id"],
                    items="admins",
                    type_factory=int,
                    on_click=on_item,
                ),
                id="admins_scroll",
                width=1,
                height=LIST_PAGE_SIZE,
                hide_on_single_page=True,
            ),
            Button(Const("➕ Добавить админа"), id="admin_add", on_click=on_add, when="owner"),
            Button(Const(BACK), id="close", on_click=on_close),
            getter=list_getter,
            state=AdminsSG.list,
            parse_mode="HTML",
        ),
        Window(
            Format("{text}"),
            Column(
                Button(
                    Const("🗑 Удалить из админов"),
                    id="admin_remove",
                    on_click=on_remove,
                    when="removable",
                ),
            ),
            Button(Const(BACK), id="item_back", on_click=on_back_to_list),
            getter=item_getter,
            state=AdminsSG.item,
            parse_mode="HTML",
        ),
        Window(
            Format("{text}"),
            Row(
                Button(Const("Да, удалить"), id="remove_yes", on_click=on_remove_confirm),
                Button(Const(NO), id="remove_no", on_click=on_back_to_item),
            ),
            getter=confirm_getter,
            state=AdminsSG.confirm_remove,
            parse_mode="HTML",
        ),
        on_process_result=on_picked,
    )
]
