"""Участники встречи в меню админа: список, карточка, добавление, удаление, бан.

Диалог запускается с `{"event_id": int}`. Выбранные запись и отложенное действие лежат в
`dialog_data`, а не в замыканиях; состояние диалогов хранится в памяти и после перезапуска
бота сбрасывается.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram.types import CallbackQuery, ContentType, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Column, Row, Select, SwitchTo
from aiogram_dialog.widgets.kbd.scrolling_group import ScrollingGroup
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.db.models import BanReason, RegistrationSource, RegistrationStatus
from meetup_bot.dialogs._buttons import BACK, NO_REASON, TO_EVENT, TO_LIST
from meetup_bot.dialogs._common import (
    WARNING_PREFIX,
    actor_id,
    error_text,
    get_services,
    guard,
    notice_block,
    picked_user,
    set_notice,
)
from meetup_bot.dialogs.states import ParticipantsSG, PickerSG
from meetup_bot.services.errors import DomainError, ErrorCode, InvalidOperation
from meetup_bot.services.names import FALLBACK_NAME
from meetup_bot.services.participants import ParticipantList, ParticipantRow
from meetup_bot.services.settings_schema import NAME_MAX_LEN, REASON_MAX_LEN
from meetup_bot.texts.render import GUEST_SUFFIX, clip, format_moment, telegram_length

logger = logging.getLogger(__name__)

_BUTTON_LIMIT = 60
NO_DM_MARK = "📵"
NO_DM_HINT = f"{NO_DM_MARK} — не нажимал(а) «Старт» у бота, личные сообщения не дойдут"

_REG, _PENDING, _ADD = "reg_id", "pending", "pending_add"
# Виды действий над записью: они же суффиксы id кнопок («ask_<вид>») и ключи текстов подтверждения
_REMOVE, _FREE, _NOSHOW = "remove", "free", "noshow"
_ASK_PREFIX = "ask_"


# ---- общие помощники ----------------------------------------------------------------


def _event_id(manager: DialogManager) -> int:
    data: Any = manager.start_data
    return int(data["event_id"])


async def _guard(event: Message | CallbackQuery, manager: DialogManager) -> bool:
    """Без прав диалог закрывается: продолжать работу с участниками нельзя."""
    return await guard(event, manager, close=True)


def _failure(error: DomainError) -> str:
    return f"{WARNING_PREFIX}{error_text(error)}"


async def _to_list(manager: DialogManager, notice: str | None = None) -> None:
    set_notice(manager, notice)
    manager.dialog_data.pop(_PENDING, None)
    manager.dialog_data.pop(_ADD, None)
    await manager.switch_to(ParticipantsSG.list)


async def _clear_notice(_: Any, __: Any, manager: DialogManager) -> None:
    set_notice(manager, None)


def _back_to_list(suffix: str) -> SwitchTo:
    return SwitchTo(
        Const(TO_LIST),
        id=f"to_list_{suffix}",
        state=ParticipantsSG.list,
        on_click=_clear_notice,
    )


def _plain_name(row: ParticipantRow) -> str:
    return row.name + (GUEST_SUFFIX if row.guests else "")


def _dm_unreachable(row: ParticipantRow) -> bool:
    # Ручной записи без аккаунта писать некому в принципе: её выдаёт пометка «(вручную)»
    return row.user_id is not None and not row.dm_available


def _row_label(row: ParticipantRow, index: int) -> str:
    if row.status == RegistrationStatus.WAITLIST.value:
        label = f"⏳ {row.position}. {_plain_name(row)}"
    else:
        label = f"{index}. {_plain_name(row)}"
    if not _dm_unreachable(row):
        return clip(label, _BUTTON_LIMIT)
    suffix = f" {NO_DM_MARK}"
    # Обрезается имя, а не пометка: иначе у длинного имени она пропала бы
    return clip(label, _BUTTON_LIMIT - telegram_length(suffix)) + suffix


async def _load_rows(manager: DialogManager) -> tuple[list[ParticipantRow], ParticipantList]:
    result = await get_services(manager).participants.list_participants(
        actor_id(manager), _event_id(manager)
    )
    return [*result.active, *result.waitlist], result


# ---- список --------------------------------------------------------------------------


async def _list_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    try:
        rows, result = await _load_rows(dialog_manager)
    except DomainError as exc:
        return {
            "text": f"<b>👥 Участники</b>\n{html.escape(_failure(exc))}",
            "items": [],
        }
    items = [
        {"id": row.registration_id, "label": _row_label(row, i)}
        for i, row in enumerate(rows, start=1)
    ]
    body = "Пока никого нет." if not rows else "Выберите участника:"
    queue = f"\nВ очереди: {len(result.waitlist)}" if result.waitlist else ""
    no_dm = f"\n{NO_DM_HINT}" if any(_dm_unreachable(row) for row in rows) else ""
    text = (
        f"<b>👥 Участники</b>\nЗанято мест: {result.seats_taken} из {result.capacity}{queue}"
        f"{no_dm}\n"
        f"{notice_block(dialog_manager)}\n{body}"
    )
    return {"text": text, "items": items}


async def _on_pick_row(
    callback: CallbackQuery, _: Any, manager: DialogManager, registration_id: int
) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_REG] = registration_id
    set_notice(manager, None)
    await manager.switch_to(ParticipantsSG.item)


async def _on_close(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    await manager.done()


async def _on_add(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    set_notice(manager, None)
    await manager.switch_to(ParticipantsSG.add_choose)


# ---- карточка участника --------------------------------------------------------------


async def _find_row(manager: DialogManager) -> tuple[ParticipantRow | None, bool]:
    """(запись, прошёл ли дедлайн отмены). Запись None, если её уже нет в живых."""
    rows, result = await _load_rows(manager)
    wanted = manager.dialog_data.get(_REG)
    row = next((r for r in rows if r.registration_id == wanted), None)
    return row, result.after_deadline


async def _item_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    empty = {"can_noshow": False, "can_ban": False, "after_deadline": False, "found": False}
    try:
        row, after_deadline = await _find_row(dialog_manager)
    except DomainError as exc:
        return {"text": html.escape(_failure(exc)), **empty}
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
        lines.append(f"С гостем{GUEST_SUFFIX}")
    source = "добавлен админом" if row.source == RegistrationSource.ADMIN.value else "сам записался"
    lines.append(f"Запись: {format_moment(row.created_at, tz)}, {source}")
    is_active = row.status == RegistrationStatus.ACTIVE.value
    return {
        "text": "\n".join(lines) + "\n" + notice_block(dialog_manager),
        "found": True,
        "after_deadline": after_deadline,
        # Неявка имеет смысл только для активной записи реального пользователя
        "can_noshow": after_deadline and is_active and row.user_id is not None,
        "can_ban": row.user_id is not None,
    }


async def _ask_remove(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_PENDING] = str(button.widget_id).removeprefix(_ASK_PREFIX)
    await manager.switch_to(ParticipantsSG.confirm_remove)


async def _on_ban_start(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    set_notice(manager, None)
    await manager.switch_to(ParticipantsSG.ban_comment)


# ---- подтверждение удаления ----------------------------------------------------------

_CONFIRM_TEXTS = {
    _REMOVE: "Удалить участника из встречи? Штрафа не будет, место освободится.",
    _FREE: "Отменить запись без штрафа? Место освободится.",
    _NOSHOW: "Отменить запись как неявку? Это учтётся в счётчике неявок и может привести к бану.",
}


async def _confirm_remove_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    kind = dialog_manager.dialog_data.get(_PENDING, _REMOVE)
    try:
        row, _late = await _find_row(dialog_manager)
    except DomainError as exc:
        return {"text": html.escape(_failure(exc))}
    who = html.escape(row.name) if row else "участника"
    return {"text": f"<b>{who}</b>\n{_CONFIRM_TEXTS.get(str(kind), _CONFIRM_TEXTS[_REMOVE])}"}


async def _on_confirm_remove(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    kind = manager.dialog_data.get(_PENDING)
    registration_id = manager.dialog_data.get(_REG)
    if kind not in _CONFIRM_TEXTS or registration_id is None:
        await _to_list(manager, f"{WARNING_PREFIX}Действие устарело, повторите.")
        return
    service = get_services(manager).participants
    try:
        if kind == _REMOVE:
            result = await service.admin_remove(
                actor_id(manager), _event_id(manager), int(registration_id)
            )
        else:
            result = await service.admin_cancel_after_deadline(
                actor_id(manager),
                _event_id(manager),
                int(registration_id),
                as_no_show=kind == _NOSHOW,
            )
    except DomainError as exc:
        await _to_list(manager, _failure(exc))
        return
    parts = ["✅ Запись отменена как неявка." if kind == _NOSHOW else "✅ Участник удалён."]
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
    except DomainError as exc:
        return {"text": html.escape(_failure(exc))}
    who = html.escape(row.name) if row else "участника"
    return {
        "text": f"<b>🚫 Забанить: {who}?</b>\n{notice_block(dialog_manager)}\n"
        f"Пришлите причину (до {REASON_MAX_LEN} символов) или нажмите «{NO_REASON}». "
        "Бан сразу вступит в силу."
    }


async def _do_ban(manager: DialogManager, comment: str | None) -> None:
    try:
        row, _late = await _find_row(manager)
        if row is None or row.user_id is None:
            await _to_list(manager, f"{WARNING_PREFIX}Эту запись забанить нельзя.")
            return
        await get_services(manager).bans.ban(
            actor_id(manager),
            row.user_id,
            reason=BanReason.MANUAL,
            comment=comment,
            event_id=_event_id(manager),
        )
        name = row.name
    except DomainError as exc:
        await _to_list(manager, _failure(exc))
        return
    await _to_list(manager, f"🚫 {name}: забанен(а).")


async def _on_ban_text(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    comment = (message.text or "").strip()
    if not comment:
        set_notice(manager, f"{WARNING_PREFIX}Пришлите причину текстом или нажмите «{NO_REASON}».")
        return
    if len(comment) > REASON_MAX_LEN:
        set_notice(
            manager,
            f"{WARNING_PREFIX}Слишком длинно: {len(comment)} из {REASON_MAX_LEN} символов.",
        )
        return
    await _do_ban(manager, comment)


async def _on_ban_no_reason(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    await _do_ban(manager, None)


# ---- добавление ----------------------------------------------------------------------


async def _add_choose_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    return {"text": f"<b>➕ Добавить участника</b>\n{notice_block(dialog_manager)}\nКого записать?"}


async def _on_pick_user(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    try:
        rows, _result = await _load_rows(manager)
    except DomainError as exc:
        # Без списка записанных пикер не смог бы исключить их: лучше показать причину
        logger.warning("не удалось получить участников для выбора: %r", exc)
        set_notice(manager, _failure(exc))
        return
    exclude = [r.user_id for r in rows if r.user_id is not None]
    set_notice(manager, None)
    await manager.start(
        PickerSG.pick, data={"title": "Кого записать на встречу?", "exclude_ids": exclude}
    )


async def _on_manual(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    set_notice(manager, None)
    await manager.switch_to(ParticipantsSG.add_manual_name)


async def _do_add(
    manager: DialogManager,
    *,
    user: dict[str, Any] | None,
    manual_name: str | None,
    force: bool,
) -> None:
    """`user` — словарь из результата выбора: в `dialog_data` кладём только простые типы."""
    try:
        await get_services(manager).participants.admin_add(
            actor_id(manager),
            _event_id(manager),
            user=picked_user(user),
            manual_name=manual_name,
            force=force,
        )
    except InvalidOperation as exc:
        if exc.code == ErrorCode.OVER_CAPACITY and not force:
            manager.dialog_data[_ADD] = {"user": user, "manual_name": manual_name}
            set_notice(manager, None)
            await manager.switch_to(ParticipantsSG.confirm_over_capacity)
            return
        await _to_list(manager, _failure(exc))
        return
    except DomainError as exc:
        await _to_list(manager, _failure(exc))
        return
    label = manual_name or (user or {}).get("first_name") or FALLBACK_NAME
    await _to_list(manager, f"✅ Добавлен(а): {label}.")


async def _on_manual_name(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    name = (message.text or "").strip()
    if not name:
        set_notice(manager, f"{WARNING_PREFIX}Имя не может быть пустым. Пример: Анна Петрова.")
        return
    if len(name) > NAME_MAX_LEN:
        set_notice(
            manager, f"{WARNING_PREFIX}Слишком длинно: {len(name)} из {NAME_MAX_LEN} символов."
        )
        return
    await _do_add(manager, user=None, manual_name=name, force=False)


async def _manual_name_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    return {
        "text": f"<b>✍️ Имя участника</b>\n{notice_block(dialog_manager)}\n"
        f"Пришлите имя (до {NAME_MAX_LEN} символов), например: Анна Петрова."
    }


async def _on_result(_start: Any, result: Any, manager: DialogManager) -> None:
    """Результат пикера: словарь пользователя; отмена приходит как None."""
    if picked_user(result) is None:
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
        await _to_list(manager, f"{WARNING_PREFIX}Действие устарело, повторите.")
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
    Button(Const(TO_EVENT), id="close_participants", on_click=_on_close),
    state=ParticipantsSG.list,
    getter=_list_getter,
)

_item_window = Window(
    _BODY,
    Column(
        Button(
            Const("Удалить из встречи"),
            id=f"{_ASK_PREFIX}{_REMOVE}",
            on_click=_ask_remove,
            when="found",
        ),
        Button(
            Const("Отменить без штрафа"),
            id=f"{_ASK_PREFIX}{_FREE}",
            on_click=_ask_remove,
            when="after_deadline",
        ),
        Button(
            Const("Отменить как неявку"),
            id=f"{_ASK_PREFIX}{_NOSHOW}",
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
        Const(BACK),
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
    Button(Const(NO_REASON), id="ban_no_reason", on_click=_on_ban_no_reason),
    SwitchTo(Const(BACK), id="back_ban", state=ParticipantsSG.item, on_click=_clear_notice),
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
