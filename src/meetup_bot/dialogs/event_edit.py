"""Окна правки встречи: описание, фото, место, ссылка, дата, лимит, моменты.

Окна входят в тот же диалог, что и карточка (`EventsSG`): их собирает `dialogs/events.py`.
Всё, что можно менять, решает и проверяет сервис; здесь только разбор ввода и показ результата.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput, TextInput
from aiogram_dialog.widgets.kbd import Button, Column, Row, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.dialogs._buttons import BACK, TO_EVENT
from meetup_bot.dialogs._common import (
    DEADLINE_CLAMPED_WARNING,
    actor_id,
    error_text,
    get_services,
    guard,
    set_notice,
)
from meetup_bot.dialogs.states import EventsSG
from meetup_bot.services.errors import DomainError
from meetup_bot.services.settings_schema import MAX_CAPACITY, MIN_CAPACITY
from meetup_bot.telegram.common import fit_alert
from meetup_bot.telegram.errors_text import describe_error
from meetup_bot.texts.render import SHORT_DATETIME, format_local

logger = logging.getLogger(__name__)

EVENT_ID_KEY = "event_id"
_MOMENT_KEY = "moment"
_PENDING_CAPACITY_KEY = "pending_capacity"

DATE_HINT = "Введите дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ, например 15.11.2026 18:30."
CAPACITY_HINT = "Введите целое число, например 12."

# Моменты из 5.2: ключ параметра edit_moments (он же поле карточки) -> подпись
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

MIN_YEAR = 2000
MAX_YEAR = 2100


# ---------------------------------------------------------------- помощники


def current_event_id(manager: DialogManager) -> int | None:
    value = manager.dialog_data.get(EVENT_ID_KEY)
    return int(value) if value is not None else None


def _tz(manager: DialogManager) -> ZoneInfo:
    return get_services(manager).settings.timezone_now()


def _esc(value: str) -> str:
    return html.escape(value, quote=False)


def parse_local(text: str, tz: ZoneInfo) -> datetime | None:
    """«ДД.ММ.ГГГГ ЧЧ:ММ» в часовом поясе клуба -> UTC; при ошибке формата None."""
    try:
        naive = datetime.strptime(text.strip(), SHORT_DATETIME)  # noqa: DTZ007
        if not MIN_YEAR <= naive.year <= MAX_YEAR:
            # Вычитание часов в compute_times у границы диапазона дало бы OverflowError
            return None
        return naive.replace(tzinfo=tz).astimezone(UTC)
    except ValueError, OverflowError:
        # OverflowError — год у границы диапазона (01.01.0001 со сдвигом пояса)
        return None


async def _alert(callback: CallbackQuery, exc: DomainError) -> None:
    await callback.answer(fit_alert(describe_error(exc)), show_alert=True)


async def _finish_edit(
    manager: DialogManager, notice: str, warnings: list[str] | None = None
) -> None:
    if warnings:
        notice += "\n" + "\n".join(warnings)
    set_notice(manager, notice)
    manager.dialog_data.pop(_MOMENT_KEY, None)
    await manager.switch_to(EventsSG.card)


# ---------------------------------------------------------------- данные окон


async def _menu_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    event_id = current_event_id(dialog_manager)
    can_reschedule = False
    if event_id is not None:
        try:
            card = await get_services(dialog_manager).events.get_card(
                actor_id(dialog_manager), event_id
            )
            can_reschedule = card.can_reschedule
        except DomainError as exc:
            logger.warning("меню правок: карточка встречи %s недоступна: %r", event_id, exc)
    return {"can_reschedule": can_reschedule}


async def _edit_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    event_id = current_event_id(dialog_manager)
    chosen = str(dialog_manager.dialog_data.get(_MOMENT_KEY, ""))
    empty: dict[str, Any] = {
        "place": "",
        "place_url": "",
        "starts": "",
        "capacity": 0,
        "moments": "",
        "chosen": chosen,
        "chosen_title": MOMENTS.get(chosen, ""),
        "has_photo": False,
        "tz": "",
    }
    if event_id is None:
        return empty
    try:
        card = await get_services(dialog_manager).events.get_card(
            actor_id(dialog_manager), event_id
        )
    except DomainError as exc:
        logger.warning("окно правки: карточка встречи %s недоступна: %r", event_id, exc)
        return empty
    tz = _tz(dialog_manager)
    moments = "\n".join(
        f"{title}: {format_local(getattr(card, name), tz, SHORT_DATETIME)}"
        for name, title in MOMENTS.items()
    )
    return empty | {
        "place": _esc(card.place) or "не указано",
        "place_url": _esc(card.place_url) or "не указана",
        "starts": format_local(card.starts_at, tz, SHORT_DATETIME),
        "capacity": card.capacity,
        "moments": moments,
        "has_photo": bool(card.photo_file_id),
        "tz": _esc(tz.key),
    }


# ---------------------------------------------------------------- описание, фото, место, ссылка


async def _update_details(
    message: Message, manager: DialogManager, notice: str, **fields: Any
) -> None:
    event_id = current_event_id(manager)
    if not await guard(message, manager, close=False) or event_id is None:
        return
    try:
        warnings = await get_services(manager).events.update_details(
            actor_id(manager), event_id, **fields
        )
    except DomainError as exc:
        await message.answer(_esc(error_text(exc)))
        return
    await _finish_edit(manager, notice, warnings)


async def _clear_field(
    callback: CallbackQuery, manager: DialogManager, notice: str, **fields: Any
) -> None:
    event_id = current_event_id(manager)
    if not await guard(callback, manager, close=False) or event_id is None:
        return
    try:
        await get_services(manager).events.update_details(actor_id(manager), event_id, **fields)
    except DomainError as exc:
        await _alert(callback, exc)
        return
    await _finish_edit(manager, notice)


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


# ---------------------------------------------------------------- дата и время


async def _on_starts(message: Message, widget: Any, manager: DialogManager, value: str) -> None:
    event_id = current_event_id(manager)
    if not await guard(message, manager, close=False) or event_id is None:
        return
    moment = parse_local(value, _tz(manager))
    if moment is None:
        await message.answer(DATE_HINT)
        return
    try:
        result = await get_services(manager).events.reschedule(actor_id(manager), event_id, moment)
    except DomainError as exc:
        await message.answer(_esc(error_text(exc)))
        return
    notice = "Встреча перенесена, записанным отправлено уведомление."
    if result.registration_reopened:
        notice += " Запись открыта заново."
    warnings = [DEADLINE_CLAMPED_WARNING] if result.deadline_clamped else None
    await _finish_edit(manager, notice, warnings)


# ---------------------------------------------------------------- лимит мест


async def _on_capacity(message: Message, widget: Any, manager: DialogManager, value: int) -> None:
    event_id = current_event_id(manager)
    if not await guard(message, manager, close=False) or event_id is None:
        return
    try:
        needs_confirmation = await get_services(manager).events.capacity_needs_confirmation(
            actor_id(manager), event_id, value
        )
    except DomainError as exc:
        await message.answer(_esc(describe_error(exc)))
        return
    if needs_confirmation:
        # Уменьшение лимита — разрушительное действие (12.2): сначала подтверждение
        manager.dialog_data[_PENDING_CAPACITY_KEY] = value
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
    event_id = current_event_id(manager)
    value = manager.dialog_data.pop(_PENDING_CAPACITY_KEY, None)
    if not await guard(callback, manager, close=False) or event_id is None or value is None:
        return

    async def notify(text: str) -> None:
        await callback.answer(fit_alert(text), show_alert=True)

    await _apply_capacity(notify, manager, event_id, int(value))


async def _capacity_confirm_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    data = await _edit_getter(dialog_manager)
    return {
        "capacity": data["capacity"],
        "pending": dialog_manager.dialog_data.get(_PENDING_CAPACITY_KEY, 0),
    }


async def _on_capacity_error(
    message: Message, widget: Any, manager: DialogManager, error: ValueError
) -> None:
    await message.answer(CAPACITY_HINT)


# ---------------------------------------------------------------- моменты


async def _on_pick_moment(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    if not await guard(callback, manager, close=False):
        return
    manager.dialog_data[_MOMENT_KEY] = item_id


async def _on_moment_back(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if manager.dialog_data.pop(_MOMENT_KEY, None) is None:
        await manager.switch_to(EventsSG.edit_menu)


async def _on_moment_value(
    message: Message, widget: Any, manager: DialogManager, value: str
) -> None:
    event_id = current_event_id(manager)
    if not await guard(message, manager, close=False) or event_id is None:
        return
    field = manager.dialog_data.get(_MOMENT_KEY)
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
        await message.answer(_esc(error_text(exc)))
        return
    await _finish_edit(manager, f"{MOMENTS[field]} обновлено.")


# ---------------------------------------------------------------- навигация


async def _on_edit_back(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data.pop(_MOMENT_KEY, None)
    await manager.switch_to(EventsSG.edit_menu)


def _back_to_edit() -> Button:
    return Button(Const(BACK), id="back", on_click=_on_edit_back)


EDIT_WINDOWS = [
    Window(
        Const("<b>Что изменить?</b>"),
        Column(
            SwitchTo(Const("Описание"), id="description", state=EventsSG.edit_description),
            SwitchTo(Const("Фото"), id="photo", state=EventsSG.edit_photo),
            SwitchTo(Const("Место"), id="place", state=EventsSG.edit_place),
            SwitchTo(Const("Ссылка на место"), id="place_url", state=EventsSG.edit_place_url),
            SwitchTo(
                Const("Дата и время"),
                id="starts",
                state=EventsSG.edit_starts,
                when="can_reschedule",
            ),
            SwitchTo(Const("Лимит мест"), id="capacity", state=EventsSG.edit_capacity),
            SwitchTo(
                Const("Моменты (окончание, отмена, закрытие, напоминание, явка)"),
                id="moments",
                state=EventsSG.edit_moments,
            ),
        ),
        SwitchTo(Const(TO_EVENT), id="back", state=EventsSG.card),
        state=EventsSG.edit_menu,
        getter=_menu_getter,
    ),
    Window(
        Const(
            "<b>Описание</b>\n\nПришлите новый текст описания одним сообщением "
            "(форматирование сохранится)."
        ),
        MessageInput(_on_description, content_types=ContentType.ANY),
        Button(Const("Очистить"), id="clear", on_click=_on_clear_description),
        _back_to_edit(),
        state=EventsSG.edit_description,
    ),
    Window(
        Const("<b>Фото</b>\n\nПришлите новое фото (как картинку, не файлом)."),
        MessageInput(_on_photo, content_types=ContentType.ANY),
        Button(Const("Убрать фото"), id="clear", on_click=_on_clear_photo, when="has_photo"),
        _back_to_edit(),
        state=EventsSG.edit_photo,
        getter=_edit_getter,
    ),
    Window(
        Format("<b>Место</b>\n\nСейчас: {place}\n\nПришлите новое название места."),
        TextInput(id="place", on_success=_on_place),
        _back_to_edit(),
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
        _back_to_edit(),
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
        _back_to_edit(),
        state=EventsSG.edit_starts,
        getter=_edit_getter,
    ),
    Window(
        Format(
            "<b>Лимит мест</b>\n\nСейчас: {capacity}.\n\n"
            f"Введите новое число от {MIN_CAPACITY} до {MAX_CAPACITY}."
        ),
        TextInput(
            id="capacity",
            type_factory=int,
            on_success=_on_capacity,
            on_error=_on_capacity_error,
        ),
        _back_to_edit(),
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
        Button(Const(BACK), id="back", on_click=_on_moment_back),
        state=EventsSG.edit_moments,
        getter=_edit_getter,
    ),
]
