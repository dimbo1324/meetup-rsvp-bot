"""Управление админами: только для владельца."""

from __future__ import annotations

import html
from typing import Any

from aiogram.types import CallbackQuery
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.kbd import Button, Column, Row, ScrollingGroup, Select
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.db.models import AdminRole
from meetup_bot.dialogs._common import (
    NO_ACCESS,
    actor_id,
    describe_error,
    get_services,
    get_sessions,
)
from meetup_bot.dialogs.states import AdminsSG, PickerSG
from meetup_bot.repositories import admins as admins_repo
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.admins import AdminEntry
from meetup_bot.telegram.common import fit_alert

_ROLES = {AdminRole.OWNER.value: "владелец", AdminRole.ADMIN.value: "админ"}


async def is_owner(manager: DialogManager) -> bool:
    async with get_sessions(manager)() as session:
        return await admins_repo.is_owner(session, actor_id(manager))


def _name(entry: AdminEntry) -> str:
    full = " ".join(p for p in (entry.first_name, entry.last_name) if p) or f"ID {entry.user_id}"
    return f"{full} (@{entry.username})" if entry.username else full


async def _entries(manager: DialogManager) -> list[AdminEntry]:
    return await get_services(manager).admins.list_admins(actor_id(manager))


async def list_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not await is_owner(dialog_manager):
        return {"text": NO_ACCESS, "admins": [], "owner": False}
    try:
        entries = await _entries(dialog_manager)
    except Exception as exc:
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
    except Exception as exc:
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
    name = ""
    if await is_owner(dialog_manager):
        entry = next((e for e in await _entries(dialog_manager) if e.user_id == user_id), None)
        name = _name(entry) if entry else f"ID {user_id}"
    return {"text": f"Удалить из админов: <b>{html.escape(name)}</b>?"}


async def on_add(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await is_owner(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    await manager.start(PickerSG.pick, data={"title": "Кого сделать админом?", "exclude_ids": []})


async def on_picked(start_data: Any, result: Any, manager: DialogManager) -> None:
    if not isinstance(result, dict) or "user_id" not in result:
        return
    if not await is_owner(manager):
        return
    info = UserInfo(
        int(result["user_id"]),
        str(result.get("first_name") or ""),
        result.get("last_name"),
        result.get("username"),
    )
    try:
        await get_services(manager).admins.add_admin(actor_id(manager), info)
    except Exception as exc:
        text = describe_error(exc)
    else:
        text = "Админ добавлен."
    event = manager.event
    if isinstance(event, CallbackQuery) and event.message is not None:
        await event.answer(fit_alert(text), show_alert=True)
    elif hasattr(event, "answer"):
        await event.answer(text)


async def on_item(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: int
) -> None:
    if not await is_owner(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    manager.dialog_data["user_id"] = item_id
    await manager.switch_to(AdminsSG.item)


async def on_remove(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await is_owner(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    await manager.switch_to(AdminsSG.confirm_remove)


async def on_remove_confirm(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    user_id = manager.dialog_data.get("user_id")
    if user_id is None or not await is_owner(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    try:
        await get_services(manager).admins.remove_admin(actor_id(manager), int(user_id))
    except Exception as exc:
        await callback.answer(fit_alert(describe_error(exc)), show_alert=True)
    else:
        await callback.answer("Админ удалён.")
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
                height=8,
                hide_on_single_page=True,
            ),
            Button(Const("➕ Добавить админа"), id="admin_add", on_click=on_add, when="owner"),
            Button(Const("⬅ Назад"), id="close", on_click=on_close),
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
            Button(Const("⬅ Назад"), id="item_back", on_click=on_back_to_list),
            getter=item_getter,
            state=AdminsSG.item,
            parse_mode="HTML",
        ),
        Window(
            Format("{text}"),
            Row(
                Button(Const("Да, удалить"), id="remove_yes", on_click=on_remove_confirm),
                Button(Const("Нет"), id="remove_no", on_click=on_back_to_item),
            ),
            getter=confirm_getter,
            state=AdminsSG.confirm_remove,
            parse_mode="HTML",
        ),
        on_process_result=on_picked,
    )
]
