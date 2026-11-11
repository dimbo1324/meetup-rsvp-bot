"""Список и карточка встречи, отмена, выгрузка и предпросмотр поста; правка — в `event_edit.py`."""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram_dialog import Data, Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Row, ScrollingGroup, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.db.models import EventStatus
from meetup_bot.dialogs._buttons import BACK, NO_REASON, TO_EVENT, TO_LIST
from meetup_bot.dialogs._common import (
    actor_id,
    get_attendance_ui,
    get_bot,
    get_services,
    guard,
    pop_notice,
    set_notice,
)
from meetup_bot.dialogs.event_edit import EDIT_WINDOWS, EVENT_ID_KEY, current_event_id
from meetup_bot.dialogs.states import CreateEventSG, EventsSG, ParticipantsSG
from meetup_bot.services.errors import DomainError, ErrorCode, NotFound
from meetup_bot.services.event_results import EventCard
from meetup_bot.services.settings_schema import REASON_MAX_LEN
from meetup_bot.telegram.attendance_ui import SheetStatus
from meetup_bot.telegram.common import fit_alert
from meetup_bot.telegram.errors_text import ERROR_TEXTS, NO_ACCESS, describe_error
from meetup_bot.texts.render import (
    ISO_DATE,
    date_parts,
    format_interval,
    format_local,
    format_moment,
)

_REASON_KEY = "reason"

STATUS_TITLES = {
    EventStatus.OPEN.value: "запись открыта",
    EventStatus.CLOSED.value: "запись закрыта",
    EventStatus.FINISHED.value: "прошла",
    EventStatus.CANCELLED.value: "отменена",
}
SHEET_TEXTS = {
    SheetStatus.SENT: "Список отправлен вам в личные сообщения.",
    SheetStatus.NOT_STARTED: ERROR_TEXTS[ErrorCode.ATTENDANCE_NOT_YET],
    SheetStatus.WINDOW_CLOSED: ERROR_TEXTS[ErrorCode.ATTENDANCE_WINDOW_CLOSED],
    SheetStatus.CANCELLED: ERROR_TEXTS[ErrorCode.EVENT_CANCELLED],
    SheetStatus.NO_ACCESS: NO_ACCESS,
    SheetStatus.FAILED: "Не удалось отправить список (напишите боту /start).",
}
# Почему кнопка «Отметить явку» сейчас не работает: единственная причина у каждого статуса
_ATTENDANCE_UNAVAILABLE = {
    EventStatus.CANCELLED.value: ERROR_TEXTS[ErrorCode.EVENT_CANCELLED],
    EventStatus.FINISHED.value: ERROR_TEXTS[ErrorCode.ATTENDANCE_WINDOW_CLOSED],
}
ATTENDANCE_MARKED_LINE = "✅ Явка отмечена"
NOT_FOUND_TEXT = "Встреча не найдена."

# Флаги кнопок карточки, когда встречи нет или к ней нет доступа: ни одно действие не показано
_NO_CARD_FLAGS = {
    "active": False,
    "editable": False,
    "can_close": False,
    "can_open": False,
    "can_cancel": False,
}


# ---------------------------------------------------------------- помощники


def _esc(value: str) -> str:
    return html.escape(value, quote=False)


def _tz(manager: DialogManager) -> ZoneInfo:
    return get_services(manager).settings.timezone_now()


async def _alert(callback: CallbackQuery, exc: DomainError) -> None:
    await callback.answer(fit_alert(describe_error(exc)), show_alert=True)


async def _load_card(manager: DialogManager) -> EventCard:
    """Карточка встречи диалога; нет id — `NotFound`, остальное решает сервис (права, встреча)."""
    event_id = current_event_id(manager)
    if event_id is None:
        raise NotFound("встреча не выбрана")
    return await get_services(manager).events.get_card(actor_id(manager), event_id)


# ---------------------------------------------------------------- список


async def _list_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    services = get_services(dialog_manager)
    tz = services.settings.timezone_now()
    try:
        found = await services.events.list_events(actor_id(dialog_manager), include_recent=True)
    except DomainError as exc:
        return {"items": [], "header": _esc(describe_error(exc))}
    items = []
    for item in found:
        _weekday, day, time_ = date_parts(item.starts_at, tz)
        status = STATUS_TITLES.get(item.status, item.status)
        label = f"{day} {time_} · {status} · занято {item.seats_taken} из {item.capacity}"
        items.append((label, item.id))
    header = "<b>Встречи</b>" if items else "<b>Встречи</b>\n\nОткрытых и недавних встреч нет."
    return {"items": items, "header": header}


async def _on_event_chosen(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: int
) -> None:
    if not await guard(callback, manager, close=False):
        return
    manager.dialog_data[EVENT_ID_KEY] = item_id
    await manager.switch_to(EventsSG.card)


async def _on_refresh(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await callback.answer("Обновлено")


async def _on_new_event(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await guard(callback, manager, close=False):
        return
    await manager.start(CreateEventSG.date)


async def _on_close_list(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.done()


async def _on_result(start_data: Data, result: Any, manager: DialogManager) -> None:
    """Мастер создания вернул id новой встречи: сразу открываем её карточку."""
    if isinstance(result, dict) and result.get("event_id") is not None:
        manager.dialog_data[EVENT_ID_KEY] = int(result["event_id"])
        await manager.switch_to(EventsSG.card)


async def _on_start(start_data: Any, manager: DialogManager) -> None:
    if isinstance(start_data, dict) and start_data.get("event_id") is not None:
        manager.dialog_data[EVENT_ID_KEY] = int(start_data["event_id"])


# ---------------------------------------------------------------- карточка


def _card_lines(card: EventCard, tz: ZoneInfo) -> list[str]:
    place = _esc(card.place) if card.place else "не указано"
    if card.place and card.place_url:
        place = f'<a href="{html.escape(card.place_url)}">{place}</a>'
    status = STATUS_TITLES.get(card.status, card.status)
    lines = [
        f"<b>{_esc(format_interval(card.starts_at, card.ends_at, tz))}</b>",
        f"Место: {place}",
        f"Статус: {status}",
        f"Занято: {card.seats_taken} из {card.capacity}, в очереди: {card.waitlist_count}",
        "",
        f"Дедлайн отмены: {format_moment(card.cancel_deadline_at, tz)}",
        f"Закрытие записи: {format_moment(card.registration_closes_at, tz)}",
        f"Напоминание: {format_moment(card.reminder_at, tz)}",
        f"Запрос явки: {format_moment(card.attendance_prompt_at, tz)}",
    ]
    if card.attendance_marked:
        lines.append(ATTENDANCE_MARKED_LINE)
    if card.status == EventStatus.CANCELLED.value and card.cancel_reason:
        lines.append(f"\nПричина отмены: {_esc(card.cancel_reason)}")
    return lines


async def _card_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    notice = pop_notice(dialog_manager)
    prefix = f"{_esc(notice)}\n\n" if notice else ""
    try:
        card = await _load_card(dialog_manager)
    except NotFound:
        return {"text": prefix + NOT_FOUND_TEXT} | _NO_CARD_FLAGS
    except DomainError as exc:
        return {"text": prefix + _esc(describe_error(exc))} | _NO_CARD_FLAGS
    return {
        "text": prefix + "\n".join(_card_lines(card, _tz(dialog_manager))),
        "active": True,
        "editable": card.editable,
        "can_close": card.can_close,
        "can_open": card.can_open,
        "can_cancel": card.can_cancel,
    }


async def _on_participants(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    event_id = current_event_id(manager)
    if not await guard(callback, manager, close=False) or event_id is None:
        return
    await manager.start(ParticipantsSG.list, data={EVENT_ID_KEY: event_id})


def _toggle_registration(open_: bool) -> Callable[..., Awaitable[None]]:
    async def on_click(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
        event_id = current_event_id(manager)
        if not await guard(callback, manager, close=False) or event_id is None:
            return
        try:
            await get_services(manager).events.set_registration_open(
                actor_id(manager), event_id, open_
            )
        except DomainError as exc:
            await _alert(callback, exc)
            return
        set_notice(manager, "Запись открыта." if open_ else "Запись закрыта.")

    return on_click


async def _on_attendance(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    event_id = current_event_id(manager)
    if not await guard(callback, manager, close=False) or event_id is None:
        return
    try:
        card = await _load_card(manager)
        if not card.can_mark_attendance:
            hint = _ATTENDANCE_UNAVAILABLE.get(
                card.status, ERROR_TEXTS[ErrorCode.ATTENDANCE_NOT_YET]
            )
            await callback.answer(fit_alert(hint), show_alert=True)
            return
        status = await get_attendance_ui(manager).send_sheet(actor_id(manager), event_id)
    except DomainError as exc:
        await _alert(callback, exc)
        return
    await callback.answer(SHEET_TEXTS.get(status, SHEET_TEXTS[SheetStatus.FAILED]), show_alert=True)


async def _on_export(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await guard(callback, manager, close=False):
        return
    tz = _tz(manager)
    try:
        card = await _load_card(manager)
        data = await get_services(manager).export.build_csv(actor_id(manager), card.id, tz.key)
    except DomainError as exc:
        await _alert(callback, exc)
        return
    filename = f"event-{format_local(card.starts_at, tz, ISO_DATE)}.csv"
    # Документ шлём в личку админа, а не «ответом»: исходное сообщение может быть недоступно
    await get_bot(manager).send_document(
        chat_id=actor_id(manager), document=BufferedInputFile(data, filename=filename)
    )
    await callback.answer("Файл отправлен.")


# ---------------------------------------------------------------- предпросмотр


async def _preview_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    event_id = current_event_id(dialog_manager)
    rendered = None
    if event_id is not None:
        rendered = await get_services(dialog_manager).posts.preview(
            actor_id(dialog_manager), event_id
        )
    if rendered is None:
        return {"text": NOT_FOUND_TEXT}
    # Текст поста уже содержит HTML-разметку и показывается как есть
    note = "\n\n<i>(к посту прикреплено фото)</i>" if rendered.has_photo else ""
    return {"text": rendered.text + note}


# ---------------------------------------------------------------- отмена встречи


async def _on_reason(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    if not await guard(message, manager, close=False):
        return
    manager.dialog_data[_REASON_KEY] = value.strip()[:REASON_MAX_LEN] or None
    await manager.switch_to(EventsSG.cancel_confirm)


async def _on_no_reason(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data[_REASON_KEY] = None
    await manager.switch_to(EventsSG.cancel_confirm)


async def _cancel_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    reason = dialog_manager.dialog_data.get(_REASON_KEY)
    shown = f"Причина: {_esc(str(reason))}" if reason else "Причина не указана."
    return {"reason_text": shown}


async def _on_cancel_confirm(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    event_id = current_event_id(manager)
    if not await guard(callback, manager, close=False) or event_id is None:
        return
    try:
        await get_services(manager).events.cancel_event(
            actor_id(manager), event_id, manager.dialog_data.get(_REASON_KEY)
        )
    except DomainError as exc:
        # Остаёмся на подтверждении: стек не залипает, кнопка «Назад» работает
        await _alert(callback, exc)
        return
    manager.dialog_data.pop(_REASON_KEY, None)
    set_notice(manager, "Встреча отменена, записанным отправлено уведомление.")
    await manager.switch_to(EventsSG.card)


DIALOGS = [
    Dialog(
        Window(
            Format("{header}"),
            ScrollingGroup(
                Select(
                    Format("{item[0]}"),
                    id="event",
                    item_id_getter=lambda item: item[1],
                    items="items",
                    type_factory=int,
                    on_click=_on_event_chosen,
                ),
                id="events_scroll",
                width=1,
                height=6,
                hide_on_single_page=True,
            ),
            Button(Const("➕ Новая встреча"), id="new", on_click=_on_new_event),
            Button(Const("🔄 Обновить"), id="refresh", on_click=_on_refresh),
            Button(Const(BACK), id="back", on_click=_on_close_list),
            state=EventsSG.list,
            getter=_list_getter,
        ),
        Window(
            Format("{text}"),
            Button(
                Const("👥 Участники"),
                id="participants",
                on_click=_on_participants,
                when="active",
            ),
            Row(
                SwitchTo(
                    Const("✏️ Редактировать"),
                    id="edit",
                    state=EventsSG.edit_menu,
                    when="editable",
                ),
                SwitchTo(
                    Const("👁 Предпросмотр"),
                    id="preview",
                    state=EventsSG.preview,
                    when="active",
                ),
            ),
            Button(
                Const("🔒 Закрыть запись"),
                id="close_reg",
                on_click=_toggle_registration(False),
                when="can_close",
            ),
            Button(
                Const("🔓 Открыть запись"),
                id="open_reg",
                on_click=_toggle_registration(True),
                when="can_open",
            ),
            Row(
                Button(
                    Const("✅ Отметить явку"),
                    id="attendance",
                    on_click=_on_attendance,
                    when="active",
                ),
                Button(
                    Const("📤 Выгрузить CSV"),
                    id="export",
                    on_click=_on_export,
                    when="active",
                ),
            ),
            SwitchTo(
                Const("❌ Отменить встречу"),
                id="cancel",
                state=EventsSG.cancel_reason,
                when="can_cancel",
            ),
            SwitchTo(Const(TO_LIST), id="to_list", state=EventsSG.list),
            state=EventsSG.card,
            getter=_card_getter,
        ),
        Window(
            Format("{text}"),
            SwitchTo(Const(TO_EVENT), id="back", state=EventsSG.card),
            state=EventsSG.preview,
            getter=_preview_getter,
        ),
        Window(
            Const("<b>Отмена встречи</b>\n\nНапишите причину отмены — её получат записавшиеся."),
            TextInput(id="reason", on_success=_on_reason),
            Button(Const(NO_REASON), id="no_reason", on_click=_on_no_reason),
            SwitchTo(Const(BACK), id="back", state=EventsSG.card),
            state=EventsSG.cancel_reason,
        ),
        Window(
            Format("<b>Отменить встречу?</b>\n\n{reason_text}\n\nЗаписанным придёт уведомление."),
            Button(Const("❌ Да, отменить"), id="confirm", on_click=_on_cancel_confirm),
            SwitchTo(Const(BACK), id="back", state=EventsSG.cancel_reason),
            state=EventsSG.cancel_confirm,
            getter=_cancel_getter,
        ),
        *EDIT_WINDOWS,
        on_start=_on_start,
        on_process_result=_on_result,
    )
]
