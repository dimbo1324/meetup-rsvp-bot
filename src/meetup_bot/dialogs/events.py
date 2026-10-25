"""Список и карточка встречи, редактирование, отмена, выгрузка и предпросмотр поста."""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.enums import ContentType
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram_dialog import Data, Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput, TextInput
from aiogram_dialog.widgets.kbd import Button, Column, Row, ScrollingGroup, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format
from sqlalchemy import func, select

from meetup_bot.db.models import Event, EventStatus, Registration, RegistrationStatus
from meetup_bot.dialogs._common import (
    NO_ACCESS,
    actor_id,
    describe_error,
    get_services,
    get_sessions,
    is_admin,
)
from meetup_bot.dialogs.states import CreateEventSG, EventsSG, ParticipantsSG
from meetup_bot.repositories import events as events_repo
from meetup_bot.services.errors import DomainError
from meetup_bot.telegram.attendance_ui import SheetStatus
from meetup_bot.telegram.common import fit_alert
from meetup_bot.telegram.posts import build_post
from meetup_bot.texts.render import date_parts, format_interval, format_moment

DATE_FORMAT = "%d.%m.%Y %H:%M"
DATE_HINT = "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ, например 15.11.2026 18:30."
REASON_LIMIT = 300

STATUS_TITLES = {
    EventStatus.OPEN.value: "запись открыта",
    EventStatus.CLOSED.value: "запись закрыта",
    EventStatus.FINISHED.value: "прошла",
    EventStatus.CANCELLED.value: "отменена",
}
SHEET_TEXTS = {
    SheetStatus.SENT: "Список отправлен вам в личные сообщения.",
    SheetStatus.NOT_STARTED: "Встреча ещё не началась.",
    SheetStatus.WINDOW_CLOSED: "Окно отметки явки закрыто (прошло более 7 суток).",
    SheetStatus.CANCELLED: "Встреча отменена.",
    SheetStatus.NO_ACCESS: NO_ACCESS,
    SheetStatus.FAILED: "Не удалось отправить список (напишите боту /start).",
}
# Моменты из 5.2: ключ параметра edit_moments (он же поле события) -> подпись
MOMENTS = {
    "ends_at": "Время окончания",
    "cancel_deadline_at": "Дедлайн отмены записи",
    "registration_closes_at": "Закрытие записи",
    "reminder_at": "Напоминание",
    "attendance_prompt_at": "Запрос явки",
}
# Порядок проверяет сервис (validate_times); подсказка избавляет от гадания после отказа
MOMENTS_ORDER_HINT = (
    "Порядок: дедлайн отмены ≤ начало ≤ закрытие записи ≤ запрос явки; окончание — позже "
    "начала. Закрытие записи может быть после начала: опоздавшие ещё успеют записаться."
)


# ---------------------------------------------------------------- помощники


def _esc(value: str) -> str:
    return html.escape(value, quote=False)


def _event_id(manager: DialogManager) -> int | None:
    value = manager.dialog_data.get("event_id")
    return int(value) if value is not None else None


async def _load_event(manager: DialogManager) -> Event | None:
    event_id = _event_id(manager)
    if event_id is None:
        return None
    async with get_sessions(manager)() as session:
        return await events_repo.get(session, event_id)


def _tz(manager: DialogManager) -> ZoneInfo:
    return get_services(manager).settings.timezone_now()


def _local_input(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime(DATE_FORMAT)


MIN_YEAR = 2000
MAX_YEAR = 2100


def parse_local(text: str, tz: ZoneInfo) -> datetime | None:
    """«ДД.ММ.ГГГГ ЧЧ:ММ» в часовом поясе клуба -> UTC; при ошибке формата None."""
    try:
        naive = datetime.strptime(text.strip(), DATE_FORMAT)  # noqa: DTZ007
        if not MIN_YEAR <= naive.year <= MAX_YEAR:
            # Вычитание часов в compute_times у границы диапазона дало бы OverflowError
            return None
        return naive.replace(tzinfo=tz).astimezone(UTC)
    except ValueError, OverflowError:
        # OverflowError — год у границы диапазона (01.01.0001 со сдвигом пояса)
        return None


def _set_notice(manager: DialogManager, text: str) -> None:
    manager.dialog_data["notice"] = text


async def _deny(callback: CallbackQuery, manager: DialogManager) -> bool:
    """True, если доступа нет (ответ уже показан)."""
    if await is_admin(manager):
        return False
    await callback.answer(NO_ACCESS, show_alert=True)
    return True


async def _deny_message(message: Message, manager: DialogManager) -> bool:
    if await is_admin(manager):
        return False
    await message.answer(NO_ACCESS)
    return True


async def _alert(callback: CallbackQuery, exc: BaseException) -> None:
    await callback.answer(fit_alert(describe_error(exc)), show_alert=True)


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
        when = f"{day} {time_}"
        status = STATUS_TITLES.get(item.status, item.status)
        label = f"{when} · {status} · занято {item.seats_taken} из {item.capacity}"
        items.append((label, item.id))
    header = "<b>Встречи</b>" if items else "<b>Встречи</b>\n\nОткрытых и недавних встреч нет."
    return {"items": items, "header": header}


async def _on_event_chosen(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: int
) -> None:
    if await _deny(callback, manager):
        return
    manager.dialog_data["event_id"] = item_id
    await manager.switch_to(EventsSG.card)


async def _on_refresh(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await callback.answer("Обновлено")


async def _on_new_event(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if await _deny(callback, manager):
        return
    await manager.start(CreateEventSG.date)


async def _on_close_list(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.done()


async def _on_result(start_data: Data, result: Any, manager: DialogManager) -> None:
    """Мастер создания вернул id новой встречи: сразу открываем её карточку."""
    if isinstance(result, dict) and result.get("event_id") is not None:
        manager.dialog_data["event_id"] = int(result["event_id"])
        await manager.switch_to(EventsSG.card)


async def _on_start(start_data: Any, manager: DialogManager) -> None:
    if isinstance(start_data, dict) and start_data.get("event_id") is not None:
        manager.dialog_data["event_id"] = int(start_data["event_id"])


# ---------------------------------------------------------------- карточка


async def _card_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    notice = dialog_manager.dialog_data.pop("notice", "")
    prefix = f"{_esc(notice)}\n\n" if notice else ""
    event = await _load_event(dialog_manager)
    if event is None:
        return {"text": prefix + "Встреча не найдена.", "found": False} | _flags(None)
    tz = _tz(dialog_manager)
    async with get_sessions(dialog_manager)() as session:
        taken = await events_repo.seats_taken(session, event.id)
        queue = await session.scalar(
            select(func.count())
            .select_from(Registration)
            .where(
                Registration.event_id == event.id,
                Registration.status == RegistrationStatus.WAITLIST.value,
            )
        )
    place = _esc(event.place) if event.place else "не указано"
    if event.place and event.place_url:
        place = f'<a href="{html.escape(event.place_url)}">{place}</a>'
    status = STATUS_TITLES.get(event.status, event.status)
    lines = [
        f"<b>{_esc(format_interval(event.starts_at, event.ends_at, tz))}</b>",
        f"Место: {place}",
        f"Статус: {status}",
        f"Занято: {taken} из {event.capacity}, в очереди: {queue or 0}",
        "",
        f"Дедлайн отмены: {format_moment(event.cancel_deadline_at, tz)}",
        f"Закрытие записи: {format_moment(event.registration_closes_at, tz)}",
        f"Напоминание: {format_moment(event.reminder_at, tz)}",
        f"Запрос явки: {format_moment(event.attendance_prompt_at, tz)}",
    ]
    if event.status == EventStatus.CANCELLED.value and event.cancel_reason:
        lines.append(f"\nПричина отмены: {_esc(event.cancel_reason)}")
    return {"text": prefix + "\n".join(lines), "found": True} | _flags(event)


def _flags(event: Event | None) -> dict[str, bool]:
    status = event.status if event else ""
    editable = status in (EventStatus.OPEN.value, EventStatus.CLOSED.value)
    return {
        "editable": editable,
        "can_close": status == EventStatus.OPEN.value,
        "can_open": status == EventStatus.CLOSED.value,
        "active": event is not None,
    }


async def _on_participants(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    event_id = _event_id(manager)
    if await _deny(callback, manager) or event_id is None:
        return
    await manager.start(ParticipantsSG.list, data={"event_id": event_id})


def _toggle_registration(open_: bool) -> Callable[..., Awaitable[None]]:
    async def on_click(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
        event_id = _event_id(manager)
        if await _deny(callback, manager) or event_id is None:
            return
        try:
            await get_services(manager).events.set_registration_open(
                actor_id(manager), event_id, open_
            )
        except DomainError as exc:
            await _alert(callback, exc)
            return
        _set_notice(manager, "Запись открыта." if open_ else "Запись закрыта.")

    return on_click


async def _on_attendance(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    event_id = _event_id(manager)
    if await _deny(callback, manager) or event_id is None:
        return
    ui = manager.middleware_data["attendance_ui"]
    try:
        status = await ui.send_sheet(actor_id(manager), event_id)
    except DomainError as exc:
        await _alert(callback, exc)
        return
    await callback.answer(SHEET_TEXTS.get(status, SHEET_TEXTS[SheetStatus.FAILED]), show_alert=True)


async def _on_export(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    event = await _load_event(manager)
    if await _deny(callback, manager) or event is None:
        return
    tz = _tz(manager)
    try:
        data = await get_services(manager).export.build_csv(actor_id(manager), event.id, tz.key)
    except DomainError as exc:
        await _alert(callback, exc)
        return
    filename = f"event-{event.starts_at.astimezone(tz):%Y-%m-%d}.csv"
    # Документ шлём в личку админа, а не «ответом»: исходное сообщение может быть недоступно
    await callback.bot.send_document(  # type: ignore[union-attr]
        chat_id=actor_id(manager), document=BufferedInputFile(data, filename=filename)
    )
    await callback.answer("Файл отправлен.")


# ---------------------------------------------------------------- предпросмотр


async def _preview_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    event_id = _event_id(dialog_manager)
    built = None
    if event_id is not None:
        built = await build_post(
            get_sessions(dialog_manager), get_services(dialog_manager).settings, event_id
        )
    if built is None:
        return {"text": "Встреча не найдена."}
    rendered = built[1]
    # Текст поста уже содержит HTML-разметку и показывается как есть
    note = "\n\n<i>(к посту прикреплено фото)</i>" if rendered.has_photo else ""
    return {"text": rendered.text + note}


# ---------------------------------------------------------------- отмена встречи


async def _on_reason(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    if await _deny_message(message, manager):
        return
    manager.dialog_data["reason"] = value.strip()[:REASON_LIMIT] or None
    await manager.switch_to(EventsSG.cancel_confirm)


async def _on_no_reason(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data["reason"] = None
    await manager.switch_to(EventsSG.cancel_confirm)


async def _cancel_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    reason = dialog_manager.dialog_data.get("reason")
    shown = f"Причина: {_esc(str(reason))}" if reason else "Причина не указана."
    return {"reason_text": shown}


async def _on_cancel_confirm(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    event_id = _event_id(manager)
    if await _deny(callback, manager) or event_id is None:
        return
    try:
        await get_services(manager).events.cancel_event(
            actor_id(manager), event_id, manager.dialog_data.get("reason")
        )
    except DomainError as exc:
        # Остаёмся на подтверждении: стек не залипает, кнопка «Назад» работает
        await _alert(callback, exc)
        return
    manager.dialog_data.pop("reason", None)
    _set_notice(manager, "Встреча отменена, записанным отправлено уведомление.")
    await manager.switch_to(EventsSG.card)


# ---------------------------------------------------------------- редактирование


async def _edit_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    event = await _load_event(dialog_manager)
    tz = _tz(dialog_manager)
    if event is None:
        return {
            "place": "",
            "place_url": "",
            "starts": "",
            "capacity": 0,
            "moments": "",
            "chosen": "",
            "chosen_title": "",
            "has_photo": False,
            "tz": "",
        }
    chosen = str(dialog_manager.dialog_data.get("moment", ""))
    moments = "\n".join(
        f"{title}: {_local_input(getattr(event, name), tz)}" for name, title in MOMENTS.items()
    )
    return {
        "place": _esc(event.place) or "не указано",
        "place_url": _esc(event.place_url) or "не указана",
        "starts": _local_input(event.starts_at, tz),
        "capacity": event.capacity,
        "moments": moments,
        "chosen": chosen,
        "chosen_title": MOMENTS.get(chosen, ""),
        "has_photo": bool(event.photo_file_id),
        "tz": _esc(tz.key),
    }


async def _finish_edit(
    manager: DialogManager, notice: str, warnings: list[str] | None = None
) -> None:
    if warnings:
        notice += "\n" + "\n".join(warnings)
    _set_notice(manager, notice)
    manager.dialog_data.pop("moment", None)
    await manager.switch_to(EventsSG.card)


async def _update_details(
    message: Message, manager: DialogManager, notice: str, **fields: Any
) -> None:
    event_id = _event_id(manager)
    if await _deny_message(message, manager) or event_id is None:
        return
    try:
        warnings = await get_services(manager).events.update_details(
            actor_id(manager), event_id, **fields
        )
    except DomainError as exc:
        await message.answer(_esc(describe_error(exc)))
        return
    await _finish_edit(manager, notice, warnings)


async def _on_description(message: Message, widget: Any, manager: DialogManager) -> None:
    if not message.text:
        await message.answer("Пришлите текст описания (можно с форматированием).")
        return
    await _update_details(
        message, manager, "Описание обновлено.", description_html=message.html_text
    )


async def _on_clear_description(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    await _clear_field(callback, manager, "Описание очищено.", description_html="")


async def _on_photo(message: Message, widget: Any, manager: DialogManager) -> None:
    if not message.photo:
        await message.answer("Пришлите фото (как картинку, не файлом).")
        return
    await _update_details(
        message, manager, "Фото обновлено.", photo_file_id=message.photo[-1].file_id
    )


async def _on_clear_photo(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _clear_field(callback, manager, "Фото убрано.", photo_file_id=None)


async def _on_place(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    await _update_details(message, manager, "Место обновлено.", place=value.strip())


async def _on_place_url(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    await _update_details(message, manager, "Ссылка обновлена.", place_url=value.strip())


async def _on_clear_url(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _clear_field(callback, manager, "Ссылка убрана.", place_url="")


async def _clear_field(
    callback: CallbackQuery, manager: DialogManager, notice: str, **fields: Any
) -> None:
    event_id = _event_id(manager)
    if await _deny(callback, manager) or event_id is None:
        return
    try:
        await get_services(manager).events.update_details(actor_id(manager), event_id, **fields)
    except DomainError as exc:
        await _alert(callback, exc)
        return
    await _finish_edit(manager, notice)


async def _on_starts(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    event_id = _event_id(manager)
    if await _deny_message(message, manager) or event_id is None:
        return
    moment = parse_local(value, _tz(manager))
    if moment is None:
        await message.answer(DATE_HINT)
        return
    try:
        result = await get_services(manager).events.reschedule(actor_id(manager), event_id, moment)
    except DomainError as exc:
        await message.answer(_esc(describe_error(exc)))
        return
    notice = "Встреча перенесена, записанным отправлено уведомление."
    if result.registration_reopened:
        notice += " Запись открыта заново."
    await _finish_edit(manager, notice)


async def _on_capacity(message: Message, widget: Any, manager: DialogManager, value: int) -> None:
    event_id = _event_id(manager)
    if await _deny_message(message, manager) or event_id is None:
        return
    async with get_sessions(manager)() as session:
        event = await events_repo.get(session, event_id)
        taken = await events_repo.seats_taken(session, event_id)
    if event is not None and taken <= value < event.capacity:
        # Уменьшение лимита — разрушительное действие (12.2): сначала подтверждение
        manager.dialog_data["pending_capacity"] = value
        await manager.switch_to(EventsSG.capacity_confirm)
        return
    await _apply_capacity(message.answer, manager, event_id, value)


async def _apply_capacity(
    notify: Callable[[str], Awaitable[Any]], manager: DialogManager, event_id: int, value: int
) -> None:
    try:
        await get_services(manager).events.set_capacity(actor_id(manager), event_id, value)
    except DomainError as exc:
        await notify(_esc(describe_error(exc)))
        return
    await _finish_edit(manager, f"Лимит мест: {value}.")


async def _on_capacity_confirm(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    event_id = _event_id(manager)
    value = manager.dialog_data.pop("pending_capacity", None)
    if await _deny(callback, manager) or event_id is None or value is None:
        return

    async def notify(text: str) -> None:
        await callback.answer(fit_alert(text), show_alert=True)

    await _apply_capacity(notify, manager, event_id, int(value))


async def _capacity_confirm_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    event = await _load_event(dialog_manager)
    return {
        "capacity": event.capacity if event is not None else 0,
        "pending": dialog_manager.dialog_data.get("pending_capacity", 0),
    }


async def _on_capacity_error(
    message: Message, widget: Any, manager: DialogManager, error: ValueError
) -> None:
    await message.answer("Введите целое число, например 12.")


async def _on_pick_moment(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    if await _deny(callback, manager):
        return
    manager.dialog_data["moment"] = item_id


async def _on_moment_back(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if manager.dialog_data.pop("moment", None) is None:
        await manager.switch_to(EventsSG.edit_menu)


async def _on_moment_value(
    message: Message, widget: Any, manager: DialogManager, value: str
) -> None:
    event_id = _event_id(manager)
    if await _deny_message(message, manager) or event_id is None:
        return
    field = manager.dialog_data.get("moment")
    if field not in MOMENTS:
        await message.answer("Сначала выберите, какой момент менять.")
        return
    moment = parse_local(value, _tz(manager))
    if moment is None:
        await message.answer(DATE_HINT)
        return
    try:
        await get_services(manager).events.edit_moments(
            actor_id(manager), event_id, **{field: moment}
        )
    except DomainError as exc:
        await message.answer(_esc(describe_error(exc)))
        return
    await _finish_edit(manager, f"{MOMENTS[field]} обновлено.")


async def _on_edit_back(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data.pop("moment", None)
    await manager.switch_to(EventsSG.edit_menu)


def _back_to_edit(id_: str) -> Button:
    return Button(Const("⬅️ Назад"), id=id_, on_click=_on_edit_back)


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
            Button(Const("⬅️ Назад"), id="back", on_click=_on_close_list),
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
                when="editable",
            ),
            SwitchTo(Const("⬅️ К списку"), id="to_list", state=EventsSG.list),
            state=EventsSG.card,
            getter=_card_getter,
        ),
        Window(
            Format("{text}"),
            SwitchTo(Const("⬅️ К встрече"), id="back", state=EventsSG.card),
            state=EventsSG.preview,
            getter=_preview_getter,
        ),
        Window(
            Const("<b>Отмена встречи</b>\n\nНапишите причину отмены — её получат записавшиеся."),
            TextInput(id="reason", on_success=_on_reason),
            Button(Const("Без причины"), id="no_reason", on_click=_on_no_reason),
            SwitchTo(Const("⬅️ Назад"), id="back", state=EventsSG.card),
            state=EventsSG.cancel_reason,
        ),
        Window(
            Format("<b>Отменить встречу?</b>\n\n{reason_text}\n\nЗаписанным придёт уведомление."),
            Button(Const("❌ Да, отменить"), id="confirm", on_click=_on_cancel_confirm),
            SwitchTo(Const("⬅️ Назад"), id="back", state=EventsSG.cancel_reason),
            state=EventsSG.cancel_confirm,
            getter=_cancel_getter,
        ),
        Window(
            Const("<b>Что изменить?</b>"),
            Column(
                SwitchTo(Const("Описание"), id="description", state=EventsSG.edit_description),
                SwitchTo(Const("Фото"), id="photo", state=EventsSG.edit_photo),
                SwitchTo(Const("Место"), id="place", state=EventsSG.edit_place),
                SwitchTo(Const("Ссылка на место"), id="place_url", state=EventsSG.edit_place_url),
                SwitchTo(Const("Дата и время"), id="starts", state=EventsSG.edit_starts),
                SwitchTo(Const("Лимит мест"), id="capacity", state=EventsSG.edit_capacity),
                SwitchTo(
                    Const("Моменты (окончание, отмена, закрытие, напоминание, явка)"),
                    id="moments",
                    state=EventsSG.edit_moments,
                ),
            ),
            SwitchTo(Const("⬅️ К встрече"), id="back", state=EventsSG.card),
            state=EventsSG.edit_menu,
        ),
        Window(
            Const(
                "<b>Описание</b>\n\nПришлите новый текст описания одним сообщением "
                "(форматирование сохранится)."
            ),
            MessageInput(_on_description, content_types=ContentType.ANY),
            Button(Const("Очистить"), id="clear", on_click=_on_clear_description),
            _back_to_edit("back"),
            state=EventsSG.edit_description,
        ),
        Window(
            Const("<b>Фото</b>\n\nПришлите новое фото (как картинку, не файлом)."),
            MessageInput(_on_photo, content_types=ContentType.ANY),
            Button(Const("Убрать фото"), id="clear", on_click=_on_clear_photo, when="has_photo"),
            _back_to_edit("back"),
            state=EventsSG.edit_photo,
            getter=_edit_getter,
        ),
        Window(
            Format("<b>Место</b>\n\nСейчас: {place}\n\nПришлите новое название места."),
            TextInput(id="place", on_success=_on_place),
            _back_to_edit("back"),
            state=EventsSG.edit_place,
            getter=_edit_getter,
        ),
        Window(
            Format(
                "<b>Ссылка на место</b>\n\nСейчас: {place_url}\n\n"
                "Пришлите ссылку, начинающуюся с https://."
            ),
            TextInput(id="place_url", on_success=_on_place_url),
            Button(Const("Убрать ссылку"), id="clear", on_click=_on_clear_url),
            _back_to_edit("back"),
            state=EventsSG.edit_place_url,
            getter=_edit_getter,
        ),
        Window(
            Format(
                "<b>Дата и время</b>\n\nСейчас: {starts} ({tz}).\n\n"
                "Введите новые дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ. Все моменты "
                "(дедлайн отмены, закрытие записи, напоминание, запрос явки) пересчитаются, "
                "окончание сдвинется вместе с началом, записанным придёт уведомление."
            ),
            TextInput(id="starts", on_success=_on_starts),
            _back_to_edit("back"),
            state=EventsSG.edit_starts,
            getter=_edit_getter,
        ),
        Window(
            Format("<b>Лимит мест</b>\n\nСейчас: {capacity}.\n\nВведите новое число от 1 до 200."),
            TextInput(
                id="capacity",
                type_factory=int,
                on_success=_on_capacity,
                on_error=_on_capacity_error,
            ),
            _back_to_edit("back"),
            state=EventsSG.edit_capacity,
            getter=_edit_getter,
        ),
        Window(
            Format(
                "<b>Уменьшить лимит мест с {capacity} до {pending}?</b>\n\n"
                "Новые записи остановятся раньше; уже записанные останутся."
            ),
            Row(
                Button(Const("✅ Подтвердить"), id="capacity_yes", on_click=_on_capacity_confirm),
                SwitchTo(Const("Отмена"), id="capacity_no", state=EventsSG.edit_capacity),
            ),
            state=EventsSG.capacity_confirm,
            getter=_capacity_confirm_getter,
        ),
        Window(
            Format("<b>Моменты</b> ({tz})\n\n{moments}\n\n" + MOMENTS_ORDER_HINT),
            Format(
                "\nЗначение для «{chosen_title}»: введите ДД.ММ.ГГГГ ЧЧ:ММ.",
                when="chosen",
            ),
            Format("\nВыберите, что изменить.", when=lambda data, w, m: not data["chosen"]),
            TextInput(id="moment_value", on_success=_on_moment_value),
            Column(
                Select(
                    Format("{item[1]}"),
                    id="moment",
                    item_id_getter=lambda item: item[0],
                    items=list(MOMENTS.items()),
                    on_click=_on_pick_moment,
                    when=lambda data, w, m: not data["chosen"],
                ),
            ),
            Button(Const("⬅️ Назад"), id="back", on_click=_on_moment_back),
            state=EventsSG.edit_moments,
            getter=_edit_getter,
        ),
        on_start=_on_start,
        on_process_result=_on_result,
    )
]
