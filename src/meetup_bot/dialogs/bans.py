"""Баны: список, ручной бан через выбор пользователя, разбан и история (раздел 5.8)."""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Column, Row, ScrollingGroup, Select
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.db.models import Ban, BanReason, User
from meetup_bot.dialogs._common import (
    NO_ACCESS,
    actor_id,
    describe_error,
    get_services,
    get_sessions,
    is_admin,
)
from meetup_bot.dialogs.states import BansSG, PickerSG
from meetup_bot.repositories import users_known
from meetup_bot.repositories.users import UserInfo
from meetup_bot.telegram.common import fit_alert, format_moment

MAX_COMMENT = 200
_HISTORY_LIMIT = 15
_REASONS = {BanReason.NO_SHOW.value: "неявка", BanReason.MANUAL.value: "вручную"}


def _name(user: User | None, user_id: int) -> str:
    if user is None or not user.first_name:
        return f"Пользователь {user_id}"
    full = " ".join(p for p in (user.first_name, user.last_name) if p)
    return f"{full} (@{user.username})" if user.username else full


def _target(manager: DialogManager) -> int | None:
    """Пользователь карточки: выбранный в списке либо переданный при запуске."""
    value = manager.dialog_data.get("user_id")
    if value is None:
        data = manager.start_data
        value = data.get("user_id") if isinstance(data, dict) else None
    return int(value) if value is not None else None


def _started_at_item(manager: DialogManager) -> bool:
    data = manager.start_data
    return isinstance(data, dict) and data.get("user_id") is not None


async def _users(manager: DialogManager, ids: set[int]) -> dict[int, User]:
    async with get_sessions(manager)() as session:
        return await users_known.get_many(session, ids)


async def list_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not await is_admin(dialog_manager):
        return {"text": NO_ACCESS, "bans": []}
    services = get_services(dialog_manager)
    try:
        bans = await services.bans.list_banned(actor_id(dialog_manager))
    except Exception as exc:
        return {"text": html.escape(describe_error(exc)), "bans": []}
    tz = services.settings.timezone_now()
    users = await _users(dialog_manager, {b.user_id for b in bans})
    items = [
        {
            "id": ban.user_id,
            "label": (
                f"{_name(users.get(ban.user_id), ban.user_id)} · "
                f"{_REASONS.get(ban.reason, ban.reason)} · {ban.banned_at.astimezone(tz):%d.%m.%Y}"
            ),
        }
        for ban in bans
    ]
    text = f"<b>Баны</b>\nСейчас в бане: {len(bans)}." if bans else "<b>Баны</b>\nБанов нет."
    return {"text": text, "bans": items}


async def item_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    user_id = _target(dialog_manager)
    if not await is_admin(dialog_manager) or user_id is None:
        return {"text": NO_ACCESS, "has_ban": False}
    services = get_services(dialog_manager)
    try:
        history = await services.bans.history(actor_id(dialog_manager), user_id)
    except Exception as exc:
        return {"text": html.escape(describe_error(exc)), "has_ban": False}
    tz = services.settings.timezone_now()
    active = next((b for b in reversed(history) if b.active), None)
    ids = {user_id} | {b.banned_by for b in history if b.banned_by is not None}
    users = await _users(dialog_manager, ids)
    head = f"<b>{html.escape(_name(users.get(user_id), user_id))}</b>\nID: {user_id}\n\n"
    if active is None:
        return {"text": head + "Сейчас пользователь не в бане.", "has_ban": False}
    lines = [
        f"Причина: {_REASONS.get(active.reason, active.reason)}",
        f"Забанен: {format_moment(active.banned_at, tz)}",
        f"Кем: {_author(active.banned_by, users)}",
    ]
    if active.comment:
        lines.append(f"Комментарий: {html.escape(active.comment)}")
    return {"text": head + "\n".join(lines), "has_ban": True}


def _author(user_id: int | None, users: dict[int, User]) -> str:
    if user_id is None:
        return "автоматически"
    return html.escape(_name(users.get(user_id), user_id))


def _history_entry(ban: Ban, users: dict[int, User], tz: Any) -> str:
    reason = _REASONS.get(ban.reason, ban.reason)
    line = (
        f"• {format_moment(ban.banned_at, tz)} — бан ({reason}), "
        f"кем: {_author(ban.banned_by, users)}"
    )
    if ban.comment:
        line += f"; {html.escape(ban.comment)}"
    if ban.unbanned_at is not None:
        how = "автоматически" if ban.auto_removed else _author(ban.unbanned_by, users)
        line += f"\n   разбан: {format_moment(ban.unbanned_at, tz)}, кем: {how}"
    elif ban.active:
        line += "\n   действует"
    return line


async def history_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    user_id = _target(dialog_manager)
    if not await is_admin(dialog_manager) or user_id is None:
        return {"text": NO_ACCESS}
    services = get_services(dialog_manager)
    try:
        history = await services.bans.history(actor_id(dialog_manager), user_id)
    except Exception as exc:
        return {"text": html.escape(describe_error(exc))}
    tz = services.settings.timezone_now()
    ids = {user_id}
    for ban in history:
        ids.update(i for i in (ban.banned_by, ban.unbanned_by) if i is not None)
    users = await _users(dialog_manager, ids)
    title = f"<b>История: {html.escape(_name(users.get(user_id), user_id))}</b>\n\n"
    if not history:
        return {"text": title + "Банов не было."}
    # Не больше последних записей: сообщение Telegram ограничено 4096 символами
    shown = history[-_HISTORY_LIMIT:]
    body = "\n".join(_history_entry(b, users, tz) for b in reversed(shown))
    return {"text": title + body}


async def ban_comment_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    label = str(dialog_manager.dialog_data.get("ban_label", ""))
    return {
        "text": (
            f"Бан: <b>{html.escape(label)}</b>\n\n"
            f"Напишите причину (до {MAX_COMMENT} символов) или нажмите «Без причины»."
        )
    }


async def confirm_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    user_id = _target(dialog_manager)
    users = await _users(dialog_manager, {user_id} if user_id is not None else set())
    name = _name(users.get(user_id), user_id) if user_id is not None else ""
    return {"text": f"Разбанить <b>{html.escape(name)}</b>?"}


async def on_ban_start(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await is_admin(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    await manager.start(PickerSG.pick, data={"title": "Кого забанить?", "exclude_ids": []})


async def on_picked(start_data: Any, result: Any, manager: DialogManager) -> None:
    """Результат выбора пользователя; при отмене (result=None) остаёмся на месте."""
    if not isinstance(result, dict) or "user_id" not in result:
        return
    if not await is_admin(manager):
        return
    info = UserInfo(
        int(result["user_id"]),
        str(result.get("first_name") or ""),
        result.get("last_name"),
        result.get("username"),
    )
    # Ранее неизвестному человеку сохраняем имя, иначе в списке банов будет только ID
    async with get_sessions(manager)() as session, session.begin():
        await users_known.remember(session, info, datetime.now(UTC))
    manager.dialog_data["ban_user_id"] = info.id
    manager.dialog_data["ban_label"] = _name(
        User(first_name=info.first_name, last_name=info.last_name, username=info.username),
        info.id,
    )
    await manager.switch_to(BansSG.ban_comment)


async def _do_ban(
    manager: DialogManager, comment: str | None, notify: Callable[[str], Awaitable[Any]]
) -> None:
    user_id = manager.dialog_data.get("ban_user_id")
    if user_id is None:
        await manager.switch_to(BansSG.list)
        return
    if not await is_admin(manager):
        await notify(NO_ACCESS)
        return
    try:
        await get_services(manager).bans.ban(
            actor_id(manager), int(user_id), reason=BanReason.MANUAL, comment=comment
        )
    except Exception as exc:
        await notify(describe_error(exc))
        await manager.switch_to(BansSG.list)
        return
    manager.dialog_data.pop("ban_user_id", None)
    manager.dialog_data["user_id"] = int(user_id)
    await notify("Пользователь забанен.")
    await manager.switch_to(BansSG.item)


def _comment(value: str) -> str:
    text = value.strip()
    if not text or len(text) > MAX_COMMENT:
        raise ValueError("bad length")
    return text


async def on_comment(message: Message, widget: Any, manager: DialogManager, comment: str) -> None:
    await _do_ban(manager, comment, message.answer)


async def on_comment_error(
    message: Message, widget: Any, manager: DialogManager, error: ValueError
) -> None:
    await message.answer(f"Причина должна быть непустой и не длиннее {MAX_COMMENT} символов.")


async def on_no_comment(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    async def notify(text: str) -> None:
        await callback.answer(fit_alert(text), show_alert=True)

    await _do_ban(manager, None, notify)


async def on_ban_cancel(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data.pop("ban_user_id", None)
    await manager.switch_to(BansSG.list)


async def on_item(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: int
) -> None:
    if not await is_admin(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    manager.dialog_data["user_id"] = item_id
    await manager.switch_to(BansSG.item)


async def on_back_to_list(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if _started_at_item(manager):
        await manager.done()
    else:
        manager.dialog_data.pop("user_id", None)
        await manager.switch_to(BansSG.list)


async def on_unban_confirm(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    user_id = _target(manager)
    if user_id is None or not await is_admin(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    try:
        ban = await get_services(manager).bans.unban(actor_id(manager), user_id)
    except Exception as exc:
        await callback.answer(fit_alert(describe_error(exc)), show_alert=True)
        await manager.switch_to(BansSG.item)
        return
    await callback.answer("Пользователь разбанен." if ban else "Бан уже снят.")
    await on_back_to_list(callback, button, manager)


async def _to(manager: DialogManager, state: Any) -> None:
    await manager.switch_to(state)


async def on_history(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _to(manager, BansSG.history)


async def on_unban(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await is_admin(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    await _to(manager, BansSG.confirm_unban)


async def on_back_to_item(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _to(manager, BansSG.item)


async def on_close(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.done()


DIALOGS = [
    Dialog(
        Window(
            Format("{text}"),
            ScrollingGroup(
                Select(
                    Format("{item[label]}"),
                    id="ban_item",
                    item_id_getter=lambda item: item["id"],
                    items="bans",
                    type_factory=int,
                    on_click=on_item,
                ),
                id="bans_scroll",
                width=1,
                height=8,
                hide_on_single_page=True,
            ),
            Button(Const("➕ Забанить"), id="ban_new", on_click=on_ban_start),
            Button(Const("⬅ Назад"), id="close", on_click=on_close),
            getter=list_getter,
            state=BansSG.list,
            parse_mode="HTML",
        ),
        Window(
            Format("{text}"),
            Column(
                Button(Const("✅ Разбанить"), id="unban", on_click=on_unban, when="has_ban"),
                Button(Const("📜 История"), id="history", on_click=on_history),
            ),
            Button(Const("⬅ Назад"), id="item_back", on_click=on_back_to_list),
            getter=item_getter,
            state=BansSG.item,
            parse_mode="HTML",
        ),
        Window(
            Format("{text}"),
            Button(Const("⬅ Назад"), id="history_back", on_click=on_back_to_item),
            getter=history_getter,
            state=BansSG.history,
            parse_mode="HTML",
        ),
        Window(
            Format("{text}"),
            TextInput(
                id="ban_comment_input",
                type_factory=_comment,
                on_success=on_comment,
                on_error=on_comment_error,
            ),
            Row(
                Button(Const("Без причины"), id="no_comment", on_click=on_no_comment),
                Button(Const("✖ Отмена"), id="ban_cancel", on_click=on_ban_cancel),
            ),
            getter=ban_comment_getter,
            state=BansSG.ban_comment,
            parse_mode="HTML",
        ),
        Window(
            Format("{text}"),
            Row(
                Button(Const("Да, разбанить"), id="unban_yes", on_click=on_unban_confirm),
                Button(Const("Нет"), id="unban_no", on_click=on_back_to_item),
            ),
            getter=confirm_getter,
            state=BansSG.confirm_unban,
            parse_mode="HTML",
        ),
        on_process_result=on_picked,
    )
]
