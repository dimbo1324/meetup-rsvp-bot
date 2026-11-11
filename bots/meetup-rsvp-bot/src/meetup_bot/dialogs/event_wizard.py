"""Мастер «➕ Новая встреча» (раздел 12.2): дата → время → место → текст → фото → лимит → итог.

Введённые значения лежат в `dialog_data` (хранилище диалогов в памяти: после перезапуска бота
окно сбрасывается), в БД до последнего шага ничего не пишется. Предпросмотр собирается тем же
рендерером, что и боевой пост, поэтому админ видит ровно то, что получат участники.

Значения «по умолчанию» снимаются один раз при открытии мастера: поля последней неотменённой
встречи (`last_event_template`), а если встреч ещё не было — общие настройки.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import CallbackQuery, ContentType, Message
from aiogram_dialog import Dialog, DialogManager, ShowMode, Window
from aiogram_dialog.api.entities import MediaAttachment, MediaId
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import (
    Button,
    Cancel,
    Column,
    Group,
    Row,
    Select,
    SwitchTo,
)
from aiogram_dialog.widgets.kbd.calendar_kbd import CalendarUserConfig
from aiogram_dialog.widgets.media import DynamicMedia
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.db.models import EventStatus
from meetup_bot.dialogs._buttons import BACK, CANCEL, USE_DEFAULT
from meetup_bot.dialogs._common import (
    DEADLINE_CLAMPED_WARNING,
    WARNING_PREFIX,
    actor_id,
    error_text,
    get_clock,
    get_services,
    guard,
    notice_block,
    set_notice,
)
from meetup_bot.dialogs.calendar_ru import RussianCalendar
from meetup_bot.dialogs.event_wizard_support import Suggestions, length_warnings, suggestions
from meetup_bot.dialogs.states import CreateEventSG
from meetup_bot.renderer.post import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    PostData,
    render_post,
    visible_length,
)
from meetup_bot.services.errors import DomainError, ErrorCode, InvalidOperation, PermissionDenied
from meetup_bot.services.event_validation import (
    validate_capacity,
    validate_description,
    validate_place,
    validate_url,
)
from meetup_bot.services.settings_schema import MAX_CAPACITY, MIN_CAPACITY
from meetup_bot.services.timing import compute_times, next_event_start
from meetup_bot.telegram.errors_text import ERROR_TEXTS, describe_error
from meetup_bot.texts.render import DATE_INPUT_FORMAT, format_moment

TOTAL_STEPS = 6
_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
_QUICK_TIMES = ("12:00", "15:00", "17:00", "18:00", "19:00", "20:00")
_CALENDAR_ID = "wizard_calendar"

# Ключи dialog_data
_DATE, _TIME, _PLACE, _URL = "date", "time", "place", "place_url"
_DESC, _PHOTO, _CAP = "description_html", "photo", "capacity"
_SUGGESTED = "suggested"

TIME_PASSED_HINT = "Выбранный момент уже прошёл. Выберите дату заново."


# ---- общие помощники ----------------------------------------------------------------


def _head(step: int, title: str) -> str:
    return f"<b>➕ Новая встреча — шаг {step} из {TOTAL_STEPS}: {title}</b>\n"


def _notice(manager: DialogManager) -> str:
    return notice_block(manager, WARNING_PREFIX)


async def _guard(event: Message | CallbackQuery, manager: DialogManager) -> bool:
    """False и закрытие мастера, если у нажавшего уже нет прав."""
    return await guard(event, manager, close=True)


def _tz(manager: DialogManager) -> ZoneInfo:
    return get_services(manager).settings.timezone_now()


def _now(manager: DialogManager) -> datetime:
    return get_clock(manager).now()


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


async def _on_start(_: Any, manager: DialogManager) -> None:
    services = get_services(manager)
    try:
        template = await services.events.last_event_template(actor_id(manager))
    except PermissionDenied:
        # Права отозвали между меню и мастером: первое же действие закроет его через _guard
        template = None
    manager.dialog_data[_SUGGESTED] = asdict(suggestions(services.settings, template))


def _suggested(manager: DialogManager) -> Suggestions:
    return Suggestions(**manager.dialog_data[_SUGGESTED])


def _post_data(manager: DialogManager, photo: str | None) -> PostData | None:
    """Пост из введённого в мастере; None, пока не выбраны дата и время."""
    starts = _starts_at(manager)
    if starts is None:
        return None
    settings = get_services(manager).settings
    data = manager.dialog_data
    return PostData(
        event_id=0,
        status=EventStatus.OPEN.value,
        starts_at=starts,
        ends_at=compute_times(starts, settings.schedule_now()).ends_at,
        timezone=settings.timezone_now().key,
        capacity=int(data.get(_CAP, _suggested(manager).capacity)),
        seats_taken=0,
        participants=(),
        description_html=data.get(_DESC, ""),
        place=data.get(_PLACE, ""),
        place_url=data.get(_URL, ""),
        has_photo=bool(photo),
        photo_file_id=photo,
        show_names=settings.get_now("show_names_in_post"),
        plus_one_enabled=settings.options_now().plus_one_enabled,
    )


def _length_warnings(manager: DialogManager) -> str:
    """Предупреждения о длине описания для поста с фото и без; пусто, если всё помещается."""
    post = _post_data(manager, None)
    lines = [f"⚠️ {html.escape(w)}" for w in length_warnings(post)] if post else []
    return "\n" + "\n".join(lines) + "\n" if lines else ""


async def _go(manager: DialogManager, state: Any) -> None:
    set_notice(manager, None)
    await manager.switch_to(state)


async def _clear_notice(_: CallbackQuery, __: Any, manager: DialogManager) -> None:
    set_notice(manager, None)


def _back(state: Any, suffix: str) -> SwitchTo:
    return SwitchTo(Const(BACK), id=f"back_{suffix}", state=state, on_click=_clear_notice)


def _cancel() -> Cancel:
    return Cancel(Const(CANCEL), id="cancel_wizard")


# ---- шаг 1: дата ---------------------------------------------------------------------


async def _calendar_config(manager: DialogManager) -> CalendarUserConfig:
    """«Сегодня» в календаре — по часовому поясу встреч, а не сервера."""
    offset = _now(manager).astimezone(_tz(manager)).utcoffset()
    assert offset is not None
    return CalendarUserConfig(timezone=timezone(offset))


async def _date_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    services = get_services(dialog_manager)
    tz = services.settings.timezone_now()
    now = _now(dialog_manager)
    calendar = dialog_manager.find(_CALENDAR_ID)
    if calendar is not None and calendar.get_offset() is None:
        # Иначе календарь открылся бы на месяце системных часов, а не часов бота
        calendar.set_offset(now.astimezone(tz).date())
    nearest = next_event_start(now, services.settings.schedule_now())
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
        set_notice(manager, "Эта дата уже прошла. Выберите сегодняшнюю или будущую.")
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
    suggested = _suggested(dialog_manager)
    picked = date.fromisoformat(dialog_manager.dialog_data[_DATE])
    return {
        "text": _head(2, "время")
        + f"Дата: <b>{picked.strftime(DATE_INPUT_FORMAT)}</b>\n"
        + _notice(dialog_manager)
        + "\nВыберите время кнопкой или пришлите его в формате <code>ЧЧ:ММ</code>, "
        f"например <code>18:30</code>. По умолчанию — {suggested.time}{suggested.source}.",
        "quick_times": list(_QUICK_TIMES),
        "default_time": suggested.time,
    }


async def _accept_time(
    event: Message | CallbackQuery, manager: DialogManager, hours: int, minutes: int
) -> None:
    manager.dialog_data[_TIME] = f"{hours:02d}:{minutes:02d}"
    starts = _starts_at(manager)
    assert starts is not None
    # Близость к началу не мешает: сервис сам приравняет прошедший дедлайн отмены к моменту создания
    if starts < _now(manager):
        manager.dialog_data.pop(_DATE, None)
        manager.dialog_data.pop(_TIME, None)
        set_notice(manager, f"{ERROR_TEXTS[ErrorCode.INVALID_TIMES]} {TIME_PASSED_HINT}")
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


async def _on_time_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    hours, minutes = map(int, _suggested(manager).time.split(":"))
    await _accept_time(callback, manager, hours, minutes)


async def _on_time_text(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    match = _TIME_RE.match(message.text or "")
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        set_notice(manager, "Не удалось разобрать время. Пример: 18:30.")
        return
    await _accept_time(message, manager, int(match.group(1)), int(match.group(2)))


# ---- шаг 3: место --------------------------------------------------------------------


async def _place_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    suggested = _suggested(dialog_manager)
    has_default = bool(suggested.place or suggested.place_url)
    if has_default:
        shown = html.escape(suggested.place or suggested.place_url) + suggested.source
    else:
        shown = "не задано"
    return {
        "text": _head(3, "место")
        + _notice(dialog_manager)
        + "\nПришлите название места. Ссылку на карту можно добавить через «|»:\n"
        "<code>Кафе «Утро» | https://maps.example.com/xyz</code>\n"
        f"Место по умолчанию: {shown}",
        "has_default_place": has_default,
    }


async def _on_place_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    suggested = _suggested(manager)
    manager.dialog_data[_PLACE] = suggested.place
    manager.dialog_data[_URL] = suggested.place_url
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
        set_notice(manager, f"{error_text(exc)} Пример: Кафе «Утро» | https://maps.example.com/xyz")
        return
    manager.dialog_data[_PLACE] = place
    manager.dialog_data[_URL] = link
    await _go(manager, CreateEventSG.description)


# ---- шаг 4: текст --------------------------------------------------------------------


def _default_hint(manager: DialogManager, what: str) -> str:
    return f"\nЕсть {what} по умолчанию{_suggested(manager).source} — кнопка «По умолчанию»."


async def _description_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    has_default = bool(_suggested(dialog_manager).description_html)
    return {
        "text": _head(4, "текст объявления")
        + _notice(dialog_manager)
        + _length_warnings(dialog_manager)
        + "\nПришлите текст встречи: форматирование (жирный, курсив, ссылки) сохранится. "
        "Или выберите кнопку." + (_default_hint(dialog_manager, "текст") if has_default else ""),
        "has_default_text": has_default,
    }


async def _on_description_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_DESC] = _suggested(manager).description_html
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
        set_notice(manager, "Пришлите текст сообщением или выберите кнопку.")
        return
    try:
        validate_description(text)
    except InvalidOperation as exc:
        set_notice(manager, error_text(exc))
        return
    manager.dialog_data[_DESC] = text
    await _go(manager, CreateEventSG.photo)


# ---- шаг 5: фото ---------------------------------------------------------------------


async def _photo_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    has_default = bool(_suggested(dialog_manager).photo_file_id)
    return {
        "text": _head(5, "фото")
        + _notice(dialog_manager)
        + _length_warnings(dialog_manager)
        + "\nПришлите фото для поста (именно фото, не файл) или пропустите шаг."
        + (_default_hint(dialog_manager, "фото") if has_default else ""),
        "has_default_photo": has_default,
    }


async def _on_photo_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_PHOTO] = _suggested(manager).photo_file_id
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
    set_notice(manager, "Это не фото. Пришлите картинку как фото или нажмите «Пропустить».")


# ---- шаг 6: лимит --------------------------------------------------------------------


async def _capacity_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    default = _suggested(dialog_manager).capacity
    return {
        "text": _head(6, "лимит мест")
        + _notice(dialog_manager)
        + f"\nПришлите число мест от {MIN_CAPACITY} до {MAX_CAPACITY} или нажмите кнопку. "
        f"По умолчанию — {default}{_suggested(dialog_manager).source}.",
        "default_capacity": default,
    }


async def _on_capacity_default(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    manager.dialog_data[_CAP] = _suggested(manager).capacity
    await _go(manager, CreateEventSG.preview)


async def _on_capacity_text(message: Message, _: Any, manager: DialogManager) -> None:
    if not await _guard(message, manager):
        return
    raw = (message.text or "").strip()
    try:
        value = validate_capacity(int(raw))
    except ValueError:
        set_notice(manager, f"Нужно целое число от {MIN_CAPACITY} до {MAX_CAPACITY}. Пример: 12.")
        return
    except InvalidOperation as exc:
        set_notice(manager, f"{describe_error(exc)} Пример: 12.")
        return
    manager.dialog_data[_CAP] = value
    await _go(manager, CreateEventSG.preview)


# ---- шаг 7: предпросмотр и создание ---------------------------------------------------


async def _preview_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    data = dialog_manager.dialog_data
    photo = data.get(_PHOTO)
    post = _post_data(dialog_manager, photo)
    if post is None or _CAP not in data:
        return {"text": "Данные неполные, начните заново.", "photo": None, "has_photo": False}
    rendered = render_post(post)
    buttons = " | ".join(b.text for row in rendered.keyboard for b in row)
    header = f"<b>👁 Предпросмотр. Всё верно?</b>\n{_notice(dialog_manager)}\n"
    footer = f"\n\n<i>Кнопки под постом: {html.escape(buttons)}</i>"
    text = header + rendered.text + footer
    # С фото окно уходит подписью (лимит 1024): пост уже занимает его целиком, подсказки
    # добавляем только если помещаются
    limit = CAPTION_LIMIT if photo else TEXT_LIMIT
    if visible_length(text) > limit:
        text = rendered.text
    media = MediaAttachment(ContentType.PHOTO, file_id=MediaId(photo)) if photo else None
    return {"text": text, "photo": media, "has_photo": media is not None}


async def _on_create(callback: CallbackQuery, _: Any, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    starts = _starts_at(manager)
    data = manager.dialog_data
    if starts is None or _CAP not in data:
        set_notice(manager, "Данные неполные, начните с даты.")
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
        if exc.code == ErrorCode.INVALID_TIMES:
            data.pop(_DATE, None)
            data.pop(_TIME, None)
            set_notice(manager, f"{error_text(exc)} Выберите дату заново.")
            await manager.switch_to(CreateEventSG.date)
            return
        set_notice(manager, error_text(exc))
        return
    except DomainError as exc:
        set_notice(manager, error_text(exc))
        return
    warnings = [*created.warnings]
    if created.deadline_clamped:
        warnings.append(DEADLINE_CLAMPED_WARNING)
    if warnings and callback.message is not None:
        lines = "\n".join(f"• {html.escape(w)}" for w in warnings)
        await callback.message.answer(f"✅ Встреча создана. Обратите внимание:\n{lines}")
    await manager.done(
        result={"event_id": created.event_id, "warnings": warnings},
        show_mode=ShowMode.SEND,
    )


# ---- сборка диалога -------------------------------------------------------------------

_BODY = Format("{text}")

_date_window = Window(
    _BODY,
    RussianCalendar(id=_CALENDAR_ID, on_click=_on_calendar, user_config=_calendar_config),
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
    Button(Format("По умолчанию ({default_time})"), id="time_default", on_click=_on_time_default),
    MessageInput(_on_time_text, content_types=ContentType.TEXT),
    Row(_back(CreateEventSG.date, "time"), _cancel()),
    state=CreateEventSG.time,
    getter=_time_getter,
)

_place_window = Window(
    _BODY,
    Button(
        Const(USE_DEFAULT),
        id="place_default",
        on_click=_on_place_default,
        when="has_default_place",
    ),
    Button(Const("Без места"), id="place_none", on_click=_on_place_none),
    MessageInput(_on_place_text, content_types=ContentType.TEXT),
    Row(_back(CreateEventSG.time, "place"), _cancel()),
    state=CreateEventSG.place,
    getter=_place_getter,
)

_description_window = Window(
    _BODY,
    Button(
        Const(USE_DEFAULT),
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
        Const(USE_DEFAULT),
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
        on_start=_on_start,
    )
]
