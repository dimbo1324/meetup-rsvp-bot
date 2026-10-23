"""Участники встречи в меню админа: список, карточка, добавление, удаление, бан.

Диалог запускается с `{"event_id": int}`. Выбранные запись и отложенное действие лежат в
`dialog_data`, а не в замыканиях: callback может прийти уже после перезапуска бота.
"""

from __future__ import annotations

import html
from typing import Any

from aiogram.types import CallbackQuery, ContentType, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Column, Row, Select, SwitchTo
from aiogram_dialog.widgets.kbd.scrolling_group import ScrollingGroup
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.db.models import BanReason, RegistrationSource, RegistrationStatus
from meetup_bot.dialogs._common import (
    NO_ACCESS,
    actor_id,
    describe_error,
    get_services,
    get_sessions,
    is_admin,
    middleware_value,
)
from meetup_bot.dialogs.states import ParticipantsSG, PickerSG
from meetup_bot.handlers._common import format_moment
from meetup_bot.repositories import events as events_repo
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.errors import InvalidOperation
from meetup_bot.services.participants import ParticipantRow

MAX_MANUAL_NAME = 100
MAX_BAN_COMMENT = 200
_BUTTON_LIMIT = 60

_NOTICE, _REG, _PENDING, _ADD = "notice", "reg_id", "pending", "pending_add"


# ---- общие помощники ----------------------------------------------------------------


def _event_id(manager: DialogManager) -> int:
    data: Any = manager.start_data
    return int(data["event_id"])


def _set_notice(manager: DialogManager, text: str | None) -> None:
    manager.dialog_data[_NOTICE] = text


def _notice(manager: DialogManager) -> str:
    text = manager.dialog_data.get(_NOTICE)
    return f"\n{html.escape(str(text))}\n" if text else ""


async def _guard(event: Message | CallbackQuery, manager: DialogManager) -> bool:
    if await is_admin(manager):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer(NO_ACCESS, show_alert=True)
    else:
        await event.answer(NO_ACCESS)
    await manager.done()
    return False


async def _to_list(manager: DialogManager, notice: str | None = None) -> None:
    _set_notice(manager, notice)
    manager.dialog_data.pop(_PENDING, None)
    manager.dialog_data.pop(_ADD, None)
    await manager.switch_to(ParticipantsSG.list)


async def _clear_notice(_: Any, __: Any, manager: DialogManager) -> None:
    _set_notice(manager, None)


def _back_to_list(suffix: str) -> SwitchTo:
    return SwitchTo(
        Const("⬅️ К списку"),
        id=f"to_list_{suffix}",
        state=ParticipantsSG.list,
        on_click=_clear_notice,
    )


def _short(text: str) -> str:
    return text if len(text) <= _BUTTON_LIMIT else text[: _BUTTON_LIMIT - 1] + "…"


def _plain_name(row: ParticipantRow) -> str:
    return row.name + (" (+1)" if row.guests else "")


def _row_label(row: ParticipantRow, index: int) -> str:
    if row.status == RegistrationStatus.WAITLIST.value:
        return _short(f"⏳ {row.position}. {_plain_name(row)}")
    return _short(f"{index}. {_plain_name(row)}")


async def _load_rows(manager: DialogManager) -> tuple[list[ParticipantRow], Any]:
    result = await get_services(manager).participants.list_participants(
        actor_id(manager), _event_id(manager)
    )
    return [*result.active, *result.waitlist], result


# ---- список --------------------------------------------------------------------------


async def _list_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    try:
        rows, result = await _load_rows(dialog_manager)
    except Exception as exc:
        return {
            "text": f"<b>👥 Участники</b>\n⚠️ {html.escape(describe_error(exc))}",
            "items": [],
        }
    items = [
        {"id": row.registration_id, "label": _row_label(row, i)}
        for i, row in enumerate(rows, start=1)
    ]
    body = "Пока никого нет." if not rows else "Выберите участника:"
    queue = f"\nВ очереди: {len(result.waitlist)}" if result.waitlist else ""
    text = (
        f"<b>👥 Участники</b>\nЗанято мест: {result.seats_taken} из {result.capacity}{queue}\n"
        f"{_notice(dialog_manager)}\n{body}"
    )
    return {"text": text, "items": items}


async def _on_pick_row(
    callback: CallbackQuery, _: Any, manager: DialogManager, registration_id: int
) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_REG] = registration_id
    _set_notice(manager, None)
    await manager.switch_to(ParticipantsSG.item)


async def _on_close(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    await manager.done()


async def _on_add(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    _set_notice(manager, None)
    await manager.switch_to(ParticipantsSG.add_choose)


# ---- карточка участника --------------------------------------------------------------


async def _find_row(manager: DialogManager) -> tuple[ParticipantRow | None, bool]:
    """(запись, прошёл ли дедлайн отмены). Запись None, если её уже нет в живых."""
    rows, _ = await _load_rows(manager)
    wanted = manager.dialog_data.get(_REG)
    row = next((r for r in rows if r.registration_id == wanted), None)
    async with get_sessions(manager)() as session:
        event = await events_repo.get(session, _event_id(manager))
    now = middleware_value(manager, "clock").now()
    after_deadline = event is not None and now > event.cancel_deadline_at
    return row, after_deadline


async def _item_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    empty = {"found": False, "can_noshow": False, "can_ban": False, "after_deadline": False}
    try:
        row, after_deadline = await _find_row(dialog_manager)
    except Exception as exc:
        return {"text": f"⚠️ {html.escape(describe_error(exc))}", **empty}
    if row is None:
        return {"text": "Эта запись уже не активна.", **empty}
    tz = get_services(dialog_manager).settings.timezone_now()
    lines = [f"<b>{html.escape(row.name)}</b>"]
    if row.username:
        lines.append(f"@{html.escape(row.username)}")
    if row.status == RegistrationStatus.WAITLIST.value:
        lines.append(f"Статус: в очереди, место {row.position}")
    else:
        lines.append("Статус: записан(а)")
    if row.guests:
        lines.append("С гостем (+1)")
    source = "добавлен админом" if row.source == RegistrationSource.ADMIN.value else "сам записался"
    lines.append(f"Запись: {format_moment(row.created_at, tz)}, {source}")
    is_active = row.status == RegistrationStatus.ACTIVE.value
    return {
        "text": "\n".join(lines) + "\n" + _notice(dialog_manager),
        "found": True,
        "after_deadline": after_deadline,
        # Неявка имеет смысл только для активной записи реального пользователя
        "can_noshow": after_deadline and is_active and row.user_id is not None,
        "can_ban": row.user_id is not None,
    }


async def _ask_remove(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_PENDING] = {
        "remove": "remove",
        "free": "free",
        "noshow": "noshow",
    }[str(button.widget_id).removeprefix("ask_")]
    await manager.switch_to(ParticipantsSG.confirm_remove)


async def _on_ban_start(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    _set_notice(manager, None)
    await manager.switch_to(ParticipantsSG.ban_comment)


# ---- подтверждение удаления ----------------------------------------------------------

_CONFIRM_TEXTS = {
    "remove": "Удалить участника из встречи? Штрафа не будет, место освободится.",
    "free": "Отменить запись без штрафа? Место освободится.",
    "noshow": "Отменить запись как неявку? Это учтётся в счётчике неявок и может привести к бану.",
}


async def _confirm_remove_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    kind = dialog_manager.dialog_data.get(_PENDING, "remove")
    try:
        row, _late = await _find_row(dialog_manager)
    except Exception as exc:
        return {"text": f"⚠️ {html.escape(describe_error(exc))}"}
    who = html.escape(row.name) if row else "участника"
    return {"text": f"<b>{who}</b>\n{_CONFIRM_TEXTS.get(str(kind), _CONFIRM_TEXTS['remove'])}"}


async def _on_confirm_remove(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    kind = manager.dialog_data.get(_PENDING)
    registration_id = manager.dialog_data.get(_REG)
    if kind not in _CONFIRM_TEXTS or registration_id is None:
        await _to_list(manager, "⚠️ Действие устарело, повторите.")
        return
    service = get_services(manager).participants
    try:
        if kind == "remove":
            result = await service.admin_remove(
                actor_id(manager), _event_id(manager), int(registration_id)
            )
        else:
            result = await service.admin_cancel_after_deadline(
                actor_id(manager),
                _event_id(manager),
                int(registration_id),
                as_no_show=kind == "noshow",
            )
    except Exception as exc:
        await _to_list(manager, f"⚠️ {describe_error(exc)}")
        return
    parts = ["✅ Запись отменена как неявка." if kind == "noshow" else "✅ Участник удалён."]
    if result.promoted:
        parts.append(f"Из очереди переведено: {len(result.promoted)}.")
    if result.banned:
        parts.append("За неявки участник автоматически забанен.")
    elif result.ban_recalc_required:
        parts.append("Неявка записана, но бан не пересчитан: проверьте чёрный список позже.")
    await _to_list(manager, " ".join(parts))


# ---- бан -----------------------------------------------------------------------------


async def _ban_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    try:
        row, _late = await _find_row(dialog_manager)
    except Exception as exc:
        return {"text": f"⚠️ {html.escape(describe_error(exc))}"}
    who = html.escape(row.name) if row else "участника"
    return {
        "text": f"<b>🚫 Забанить: {who}?</b>\n{_notice(dialog_manager)}\n"
        f"Пришлите причину (до {MAX_BAN_COMMENT} символов) или нажмите «Без причины». "
        "Бан сразу вступит в силу."
    }


async def _do_ban(manager: DialogManager, comment: str | None) -> None:
    try:
        row, _late = await _find_row(manager)
        if row is None or row.user_id is None:
            await _to_list(manager, "⚠️ Эту запись забанить нельзя.")
            return
        await get_services(manager).bans.ban(
            actor_id(manager),
            row.user_id,
            reason=BanReason.MANUAL,
            comment=comment,
            event_id=_event_id(manager),
        )
        name = row.name
    except Exception as exc:
        await _to_list(manager, f"⚠️ {describe_error(exc)}")
        return
    await _to_list(manager, f"🚫 {name}: забанен(а).")


async def _on_ban_text(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    comment = (message.text or "").strip()
    if not comment:
        _set_notice(manager, "⚠️ Пришлите причину текстом или нажмите «Без причины».")
        return
    if len(comment) > MAX_BAN_COMMENT:
        _set_notice(manager, f"⚠️ Слишком длинно: {len(comment)} из {MAX_BAN_COMMENT} символов.")
        return
    await _do_ban(manager, comment)


async def _on_ban_no_reason(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    await _do_ban(manager, None)


# ---- добавление ----------------------------------------------------------------------


async def _add_choose_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    return {"text": f"<b>➕ Добавить участника</b>\n{_notice(dialog_manager)}\nКого записать?"}


async def _on_pick_user(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    try:
        rows, _result = await _load_rows(manager)
        exclude = [r.user_id for r in rows if r.user_id is not None]
    except Exception:
        exclude = []
    await manager.start(
        PickerSG.pick, data={"title": "Кого записать на встречу?", "exclude_ids": exclude}
    )


async def _on_manual(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    _set_notice(manager, None)
    await manager.switch_to(ParticipantsSG.add_manual_name)


async def _do_add(
    manager: DialogManager,
    *,
    user: dict[str, Any] | None,
    manual_name: str | None,
    force: bool,
) -> None:
    info = (
        UserInfo(
            id=int(user["user_id"]),
            first_name=str(user.get("first_name") or ""),
            last_name=user.get("last_name"),
            username=user.get("username"),
        )
        if user
        else None
    )
    try:
        await get_services(manager).participants.admin_add(
            actor_id(manager),
            _event_id(manager),
            user=info,
            manual_name=manual_name,
            force=force,
        )
    except InvalidOperation as exc:
        if exc.code == "over_capacity" and not force:
            manager.dialog_data[_ADD] = {"user": user, "manual_name": manual_name}
            _set_notice(manager, None)
            await manager.switch_to(ParticipantsSG.confirm_over_capacity)
            return
        await _to_list(manager, f"⚠️ {describe_error(exc)}")
        return
    except Exception as exc:
        await _to_list(manager, f"⚠️ {describe_error(exc)}")
        return
    label = manual_name or (user or {}).get("first_name") or "Участник"
    await _to_list(manager, f"✅ Добавлен(а): {label}.")


async def _on_manual_name(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    name = (message.text or "").strip()
    if not name:
        _set_notice(manager, "⚠️ Имя не может быть пустым. Пример: Анна Петрова.")
        return
    if len(name) > MAX_MANUAL_NAME:
        _set_notice(manager, f"⚠️ Слишком длинно: {len(name)} из {MAX_MANUAL_NAME} символов.")
        return
    await _do_add(manager, user=None, manual_name=name, force=False)


async def _manual_name_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    return {
        "text": f"<b>✍️ Имя участника</b>\n{_notice(dialog_manager)}\n"
        f"Пришлите имя (до {MAX_MANUAL_NAME} символов), например: Анна Петрова."
    }


async def _on_result(_start: Any, result: Any, manager: DialogManager) -> None:
    """Результат пикера: словарь пользователя; отмена приходит как None."""
    if not isinstance(result, dict) or "user_id" not in result:
        return
    if not await _guard(manager.event, manager):  # type: ignore[arg-type]
        return
    await _do_add(manager, user=result, manual_name=None, force=False)


async def _over_capacity_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    return {"text": "<b>Мест не хватает.</b>\nДобавить сверх лимита?"}


async def _on_force_add(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    pending = manager.dialog_data.get(_ADD)
    if not pending:
        await _to_list(manager, "⚠️ Действие устарело, повторите.")
        return
    await _do_add(
        manager, user=pending.get("user"), manual_name=pending.get("manual_name"), force=True
    )


# ---- сборка диалога ------------------------------------------------------------------

_BODY = Format("{text}")

_list_window = Window(
    _BODY,
    ScrollingGroup(
        Select(
            Format("{item[label]}"),
            id="participant",
            item_id_getter=lambda item: item["id"],
            items="items",
            type_factory=int,
            on_click=_on_pick_row,
        ),
        id="participants_scroll",
        width=1,
        height=8,
        hide_on_single_page=True,
    ),
    Button(Const("➕ Добавить участника"), id="add_participant", on_click=_on_add),
    Button(Const("⬅️ К встрече"), id="close_participants", on_click=_on_close),
    state=ParticipantsSG.list,
    getter=_list_getter,
)

_item_window = Window(
    _BODY,
    Column(
        Button(
            Const("Удалить из встречи"),
            id="ask_remove",
            on_click=_ask_remove,
            when="found",
        ),
        Button(
            Const("Отменить без штрафа"),
            id="ask_free",
            on_click=_ask_remove,
            when="after_deadline",
        ),
        Button(
            Const("Отменить как неявку"),
            id="ask_noshow",
            on_click=_ask_remove,
            when="can_noshow",
        ),
        Button(Const("🚫 Забанить"), id="ban_start", on_click=_on_ban_start, when="can_ban"),
    ),
    _back_to_list("item"),
    state=ParticipantsSG.item,
    getter=_item_getter,
)

_add_choose_window = Window(
    _BODY,
    Column(
        Button(Const("Выбрать пользователя Telegram"), id="pick_user", on_click=_on_pick_user),
        Button(Const("Ввести имя вручную"), id="manual_name", on_click=_on_manual),
    ),
    _back_to_list("add"),
    state=ParticipantsSG.add_choose,
    getter=_add_choose_getter,
)

_manual_window = Window(
    _BODY,
    MessageInput(_on_manual_name, content_types=ContentType.TEXT),
    SwitchTo(
        Const("⬅️ Назад"),
        id="back_manual",
        state=ParticipantsSG.add_choose,
        on_click=_clear_notice,
    ),
    state=ParticipantsSG.add_manual_name,
    getter=_manual_name_getter,
)

_over_window = Window(
    _BODY,
    Button(Const("Добавить сверх лимита"), id="force_add", on_click=_on_force_add),
    _back_to_list("over"),
    state=ParticipantsSG.confirm_over_capacity,
    getter=_over_capacity_getter,
)

_confirm_window = Window(
    _BODY,
    Row(
        Button(Const("✅ Да"), id="confirm_remove", on_click=_on_confirm_remove),
        SwitchTo(
            Const("⬅️ Нет"),
            id="back_confirm",
            state=ParticipantsSG.item,
            on_click=_clear_notice,
        ),
    ),
    state=ParticipantsSG.confirm_remove,
    getter=_confirm_remove_getter,
)

_ban_window = Window(
    _BODY,
    MessageInput(_on_ban_text, content_types=ContentType.TEXT),
    Button(Const("Без причины"), id="ban_no_reason", on_click=_on_ban_no_reason),
    SwitchTo(Const("⬅️ Назад"), id="back_ban", state=ParticipantsSG.item, on_click=_clear_notice),
    state=ParticipantsSG.ban_comment,
    getter=_ban_getter,
)

DIALOGS: list[Dialog] = [
    Dialog(
        _list_window,
        _item_window,
        _add_choose_window,
        _manual_window,
        _over_window,
        _confirm_window,
        _ban_window,
        on_process_result=_on_result,
    )
]
