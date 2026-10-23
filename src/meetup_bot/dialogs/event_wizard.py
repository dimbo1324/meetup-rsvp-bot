"""Мастер «➕ Новая встреча» (раздел 12.2): дата → время → место → текст → фото → лимит → итог.

Введённые значения лежат в `dialog_data` (хранилище диалогов в памяти: после перезапуска бота
окно сбрасывается), в БД до последнего шага ничего не пишется. Предпросмотр собирается тем же
рендерером, что и боевой пост, поэтому админ видит ровно то, что получат участники.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import CallbackQuery, ContentType, Message
from aiogram_dialog import Dialog, DialogManager, ShowMode, Window
from aiogram_dialog.api.entities import MediaAttachment, MediaId
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import (
    Button,
    Calendar,
    Cancel,
    Column,
    Group,
    Row,
    Select,
    SwitchTo,
)
from aiogram_dialog.widgets.media import DynamicMedia
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.dialogs._common import (
    ERROR_TEXTS,
    NO_ACCESS,
    actor_id,
    describe_error,
    get_services,
    is_admin,
    middleware_value,
)
from meetup_bot.dialogs.states import CreateEventSG
from meetup_bot.renderer.post import PostData, render_post
from meetup_bot.services.errors import InvalidOperation
from meetup_bot.services.event_validation import (
    MAX_CAPACITY,
    validate_capacity,
    validate_description,
    validate_place,
    validate_url,
)
from meetup_bot.services.timing import compute_times, next_event_start, validate_times
from meetup_bot.telegram.common import format_moment

TOTAL_STEPS = 6
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
_QUICK_TIMES = ("12:00", "15:00", "17:00", "18:00", "19:00", "20:00")

# Ключи dialog_data
_DATE, _TIME, _PLACE, _URL = "date", "time", "place", "place_url"
_DESC, _PHOTO, _CAP, _NOTICE = "description_html", "photo", "capacity", "notice"


# ---- общие помощники ----------------------------------------------------------------


def _head(step: int, title: str) -> str:
    return f"<b>➕ Новая встреча — шаг {step} из {TOTAL_STEPS}: {title}</b>\n"


def _notice(manager: DialogManager) -> str:
    text = manager.dialog_data.get(_NOTICE)
    return f"\n⚠️ {html.escape(str(text))}\n" if text else ""


def _set_notice(manager: DialogManager, text: str | None) -> None:
    manager.dialog_data[_NOTICE] = text


async def _guard(event: Message | CallbackQuery, manager: DialogManager) -> bool:
    """False и закрытие мастера, если у нажавшего уже нет прав."""
    if await is_admin(manager):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer(NO_ACCESS, show_alert=True)
    else:
        await event.answer(NO_ACCESS)
    await manager.done()
    return False


def _tz(manager: DialogManager) -> ZoneInfo:
    return get_services(manager).settings.timezone_now()


def _now(manager: DialogManager) -> datetime:
    clock = middleware_value(manager, "clock")
    now: datetime = clock.now()
    return now


def _starts_at(manager: DialogManager) -> datetime | None:
    """Начало встречи в UTC из выбранной локальной даты и времени."""
    data = manager.dialog_data
    if not data.get(_DATE) or not data.get(_TIME):
        return None
    hours, minutes = map(int, str(data[_TIME]).split(":"))
    local = datetime.combine(
        date.fromisoformat(data[_DATE]), time(hours, minutes), tzinfo=_tz(manager)
    )
    return local.astimezone(UTC)


async def _go(manager: DialogManager, state: Any) -> None:
    _set_notice(manager, None)
    await manager.switch_to(state)


async def _clear_notice(_: CallbackQuery, __: Any, manager: DialogManager) -> None:
    _set_notice(manager, None)


def _back(state: Any, suffix: str) -> SwitchTo:
    return SwitchTo(Const("⬅️ Назад"), id=f"back_{suffix}", state=state, on_click=_clear_notice)


def _cancel() -> Cancel:
    return Cancel(Const("✖️ Отмена"), id="cancel_wizard")


# ---- шаг 1: дата ---------------------------------------------------------------------


async def _date_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    services = get_services(dialog_manager)
    tz = services.settings.timezone_now()
    nearest = next_event_start(_now(dialog_manager), services.settings.schedule_now())
    return {
        "text": _head(1, "дата")
        + _notice(dialog_manager)
        + "\nВыберите дату в календаре или возьмите ближайшую по расписанию.",
        "nearest": format_moment(nearest, tz).rsplit(",", 1)[0],
    }


async def _accept_date(
    event: Message | CallbackQuery, manager: DialogManager, picked: date
) -> None:
    if picked < _now(manager).astimezone(_tz(manager)).date():
        _set_notice(manager, "Эта дата уже прошла. Выберите сегодняшнюю или будущую.")
        return
    manager.dialog_data[_DATE] = picked.isoformat()
    await _go(manager, CreateEventSG.time)


async def _on_calendar(event: Any, _widget: Any, manager: DialogManager, picked: date) -> None:
    if not await _guard(event, manager):
        return
    await _accept_date(event, manager, picked)


async def _on_nearest(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    services = get_services(manager)
    nearest = next_event_start(_now(manager), services.settings.schedule_now())
    await _accept_date(callback, manager, nearest.astimezone(_tz(manager)).date())


# ---- шаг 2: время --------------------------------------------------------------------


async def _time_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    schedule = get_services(dialog_manager).settings.schedule_now()
    default = f"{schedule.event_time:%H:%M}"
    picked = date.fromisoformat(dialog_manager.dialog_data[_DATE])
    quick = sorted({*_QUICK_TIMES, default})
    return {
        "text": _head(2, "время")
        + f"Дата: <b>{picked:%d.%m.%Y}</b>\n"
        + _notice(dialog_manager)
        + f"\nВыберите время кнопкой или пришлите его в формате <code>ЧЧ:ММ</code>, "
        f"например <code>18:30</code>. По умолчанию — {default}.",
        "quick_times": quick,
        "default_time": default,
    }


async def _accept_time(
    event: Message | CallbackQuery, manager: DialogManager, hours: int, minutes: int
) -> None:
    manager.dialog_data[_TIME] = f"{hours:02d}:{minutes:02d}"
    starts = _starts_at(manager)
    assert starts is not None
    services = get_services(manager)
    now = _now(manager)
    try:
        validate_times(compute_times(starts, services.settings.schedule_now()), now)
    except ValueError:
        # Дата/время в прошлом либо слишком близко: дедлайны и закрытие записи не помещаются
        manager.dialog_data.pop(_DATE, None)
        manager.dialog_data.pop(_TIME, None)
        _set_notice(
            manager,
            f"{ERROR_TEXTS['invalid_times']} Скорее всего, момент уже прошёл или слишком "
            "близко (запись закрывается заранее). Выберите дату заново.",
        )
        await manager.switch_to(CreateEventSG.date)
        return
    await _go(manager, CreateEventSG.place)


async def _on_quick_time(
    callback: CallbackQuery, _: Any, manager: DialogManager, value: str
) -> None:
    if not await _guard(callback, manager):
        return
    hours, minutes = map(int, value.split(":"))
    await _accept_time(callback, manager, hours, minutes)


async def _on_time_text(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    match = _TIME_RE.match(message.text or "")
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        _set_notice(manager, "Не удалось разобрать время. Пример: 18:30.")
        return
    await _accept_time(message, manager, int(match.group(1)), int(match.group(2)))


# ---- шаг 3: место --------------------------------------------------------------------


async def _place_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    defaults = get_services(dialog_manager).settings.defaults_now()
    shown = html.escape(defaults.place) if defaults.place else "не задано"
    return {
        "text": _head(3, "место")
        + _notice(dialog_manager)
        + "\nПришлите название места. Ссылку на карту можно добавить через «|»:\n"
        "<code>Кафе «Утро» | https://maps.example.com/xyz</code>\n"
        f"Место по умолчанию: {shown}",
    }


async def _on_place_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    defaults = get_services(manager).settings.defaults_now()
    manager.dialog_data[_PLACE] = defaults.place
    manager.dialog_data[_URL] = defaults.place_url
    await _go(manager, CreateEventSG.description)


async def _on_place_none(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_PLACE] = ""
    manager.dialog_data[_URL] = ""
    await _go(manager, CreateEventSG.description)


async def _on_place_text(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    name, _sep, url = (message.text or "").partition("|")
    try:
        place = validate_place(name.strip())
        link = validate_url(url.strip())
    except InvalidOperation as exc:
        _set_notice(
            manager, f"{describe_error(exc)} Пример: Кафе «Утро» | https://maps.example.com/xyz"
        )
        return
    manager.dialog_data[_PLACE] = place
    manager.dialog_data[_URL] = link
    await _go(manager, CreateEventSG.description)


# ---- шаг 4: текст --------------------------------------------------------------------


async def _description_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    defaults = get_services(dialog_manager).settings.defaults_now()
    return {
        "text": _head(4, "текст объявления")
        + _notice(dialog_manager)
        + "\nПришлите текст встречи: форматирование (жирный, курсив, ссылки) сохранится. "
        "Или выберите кнопку.",
        "has_default_text": bool(defaults.description_html),
    }


async def _on_description_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    defaults = get_services(manager).settings.defaults_now()
    manager.dialog_data[_DESC] = defaults.description_html
    await _go(manager, CreateEventSG.photo)


async def _on_description_none(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_DESC] = ""
    await _go(manager, CreateEventSG.photo)


async def _on_description_text(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    text = (message.html_text or "").strip()
    if not text:
        _set_notice(manager, "Пришлите текст сообщением или выберите кнопку.")
        return
    try:
        validate_description(text)
    except InvalidOperation as exc:
        _set_notice(manager, describe_error(exc))
        return
    manager.dialog_data[_DESC] = text
    await _go(manager, CreateEventSG.photo)


# ---- шаг 5: фото ---------------------------------------------------------------------


async def _photo_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    defaults = get_services(dialog_manager).settings.defaults_now()
    return {
        "text": _head(5, "фото")
        + _notice(dialog_manager)
        + "\nПришлите фото для поста (именно фото, не файл) или пропустите шаг.",
        "has_default_photo": bool(defaults.photo_file_id),
    }


async def _on_photo_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    defaults = get_services(manager).settings.defaults_now()
    manager.dialog_data[_PHOTO] = defaults.photo_file_id
    await _go(manager, CreateEventSG.capacity)


async def _on_photo_skip(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_PHOTO] = None
    await _go(manager, CreateEventSG.capacity)


async def _on_photo(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    if not message.photo:
        return
    # Последний элемент — самый крупный размер
    manager.dialog_data[_PHOTO] = message.photo[-1].file_id
    await _go(manager, CreateEventSG.capacity)


async def _on_photo_wrong(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    _set_notice(manager, "Это не фото. Пришлите картинку как фото или нажмите «Пропустить».")


# ---- шаг 6: лимит --------------------------------------------------------------------


async def _capacity_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    defaults = get_services(dialog_manager).settings.defaults_now()
    return {
        "text": _head(6, "лимит мест")
        + _notice(dialog_manager)
        + f"\nПришлите число мест от 1 до {MAX_CAPACITY} или нажмите кнопку.",
        "default_capacity": defaults.capacity,
    }


async def _on_capacity_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_CAP] = get_services(manager).settings.defaults_now().capacity
    await _go(manager, CreateEventSG.preview)


async def _on_capacity_text(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    raw = (message.text or "").strip()
    try:
        value = validate_capacity(int(raw))
    except ValueError:
        _set_notice(manager, f"Нужно целое число от 1 до {MAX_CAPACITY}. Пример: 12.")
        return
    except InvalidOperation as exc:
        _set_notice(manager, f"{describe_error(exc)} Пример: 12.")
        return
    manager.dialog_data[_CAP] = value
    await _go(manager, CreateEventSG.preview)


# ---- шаг 7: предпросмотр и создание ---------------------------------------------------


async def _preview_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    services = get_services(dialog_manager)
    data = dialog_manager.dialog_data
    starts = _starts_at(dialog_manager)
    if starts is None or _CAP not in data:
        return {"text": "Данные неполные, начните заново.", "photo": None, "has_photo": False}
    photo = data.get(_PHOTO)
    post = PostData(
        event_id=0,
        status="open",
        starts_at=starts,
        timezone=services.settings.timezone_now().key,
        capacity=int(data[_CAP]),
        seats_taken=0,
        participants=(),
        description_html=data.get(_DESC, ""),
        place=data.get(_PLACE, ""),
        place_url=data.get(_URL, ""),
        has_photo=bool(photo),
        photo_file_id=photo,
        show_names=services.settings.get_now("show_names_in_post"),
        plus_one_enabled=services.settings.options_now().plus_one_enabled,
    )
    rendered = render_post(post)
    buttons = " | ".join(b.text for row in rendered.keyboard for b in row)
    text = (
        "<b>👁 Предпросмотр. Всё верно?</b>\n"
        f"{_notice(dialog_manager)}\n"
        f"{rendered.text}\n\n"
        f"<i>Кнопки под постом: {html.escape(buttons)}</i>"
    )
    media = MediaAttachment(ContentType.PHOTO, file_id=MediaId(photo)) if photo else None
    return {"text": text, "photo": media, "has_photo": media is not None}


async def _on_create(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    starts = _starts_at(manager)
    data = manager.dialog_data
    if starts is None or _CAP not in data:
        _set_notice(manager, "Данные неполные, начните с даты.")
        await manager.switch_to(CreateEventSG.date)
        return
    try:
        created = await get_services(manager).events.create_event_as_admin(
            actor_id(manager),
            starts_at=starts,
            capacity=int(data[_CAP]),
            description_html=data.get(_DESC, ""),
            place=data.get(_PLACE, ""),
            place_url=data.get(_URL, ""),
            photo_file_id=data.get(_PHOTO),
        )
    except InvalidOperation as exc:
        if exc.code == "invalid_times":
            data.pop(_DATE, None)
            data.pop(_TIME, None)
            _set_notice(manager, f"{describe_error(exc)} Выберите дату заново.")
            await manager.switch_to(CreateEventSG.date)
            return
        _set_notice(manager, describe_error(exc))
        return
    except Exception as exc:
        _set_notice(manager, describe_error(exc))
        return
    if created.warnings and callback.message is not None:
        lines = "\n".join(f"• {html.escape(w)}" for w in created.warnings)
        await callback.message.answer(f"✅ Встреча создана. Обратите внимание:\n{lines}")
    await manager.done(
        result={"event_id": created.event_id, "warnings": list(created.warnings)},
        show_mode=ShowMode.SEND,
    )


# ---- сборка диалога -------------------------------------------------------------------

_BODY = Format("{text}")

_date_window = Window(
    _BODY,
    Calendar(id="wizard_calendar", on_click=_on_calendar),
    Button(Format("📅 Ближайшая: {nearest}"), id="nearest_date", on_click=_on_nearest),
    _cancel(),
    state=CreateEventSG.date,
    getter=_date_getter,
)

_time_window = Window(
    _BODY,
    Group(
        Select(
            Format("{item}"),
            id="quick_time",
            item_id_getter=lambda item: item,
            items="quick_times",
            on_click=_on_quick_time,
        ),
        width=3,
    ),
    MessageInput(_on_time_text, content_types=ContentType.TEXT),
    Row(_back(CreateEventSG.date, "time"), _cancel()),
    state=CreateEventSG.time,
    getter=_time_getter,
)

_place_window = Window(
    _BODY,
    Button(Const("По умолчанию"), id="place_default", on_click=_on_place_default),
    Button(Const("Без места"), id="place_none", on_click=_on_place_none),
    MessageInput(_on_place_text, content_types=ContentType.TEXT),
    Row(_back(CreateEventSG.time, "place"), _cancel()),
    state=CreateEventSG.place,
    getter=_place_getter,
)

_description_window = Window(
    _BODY,
    Button(
        Const("По умолчанию"),
        id="desc_default",
        on_click=_on_description_default,
        when="has_default_text",
    ),
    Button(Const("Без текста"), id="desc_none", on_click=_on_description_none),
    MessageInput(_on_description_text, content_types=ContentType.TEXT),
    Row(_back(CreateEventSG.place, "desc"), _cancel()),
    state=CreateEventSG.description,
    getter=_description_getter,
)

_photo_window = Window(
    _BODY,
    Button(
        Const("По умолчанию"),
        id="photo_default",
        on_click=_on_photo_default,
        when="has_default_photo",
    ),
    Button(Const("Пропустить"), id="photo_skip", on_click=_on_photo_skip),
    MessageInput(_on_photo, content_types=ContentType.PHOTO),
    MessageInput(_on_photo_wrong, content_types=ContentType.ANY),
    Row(_back(CreateEventSG.description, "photo"), _cancel()),
    state=CreateEventSG.photo,
    getter=_photo_getter,
)

_capacity_window = Window(
    _BODY,
    Button(
        Format("По умолчанию ({default_capacity})"),
        id="cap_default",
        on_click=_on_capacity_default,
    ),
    MessageInput(_on_capacity_text, content_types=ContentType.TEXT),
    Row(_back(CreateEventSG.photo, "cap"), _cancel()),
    state=CreateEventSG.capacity,
    getter=_capacity_getter,
)

_preview_window = Window(
    DynamicMedia("photo", when="has_photo"),
    _BODY,
    Column(Button(Const("✅ Создать"), id="create_event", on_click=_on_create)),
    Row(_back(CreateEventSG.capacity, "preview"), _cancel()),
    state=CreateEventSG.preview,
    getter=_preview_getter,
)

DIALOGS: list[Dialog] = [
    Dialog(
        _date_window,
        _time_window,
        _place_window,
        _description_window,
        _photo_window,
        _capacity_window,
        _preview_window,
    )
]
