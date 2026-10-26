"""Меню админа, ветка «Настройки» (раздел 12.2): группы настроек, тексты, часовой пояс.

Состояние окна держим в `dialog_data`: "group" (ключ группы), "key" (редактируемая
настройка), "notice" (одноразовое сообщение о результате, готовый HTML).
"""

from __future__ import annotations

import html
import string
from typing import Any

from aiogram.enums import ContentType
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Group, Select, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.dialogs._buttons import BACK, RESET_DEFAULT, TO_MENU
from meetup_bot.dialogs._common import actor_id, get_services, guard, pop_notice, set_notice
from meetup_bot.dialogs.settings_view import (
    GROUP_DEFAULTS,
    GROUP_RULES,
    GROUP_SCHEDULE,
    GROUPS,
    PHOTO_KEY,
    POPULAR_TIMEZONES,
    TIMEZONE_KEY,
    WEEKDAY_KEYS,
    accepts_input,
    button_label,
    can_clear,
    edit_body,
    label_of,
    with_notice,
)
from meetup_bot.dialogs.states import SettingsSG
from meetup_bot.services.errors import DomainError
from meetup_bot.services.settings_schema import (
    POPUP_MAX_LEN,
    SAMPLE_VARS,
    SCHEMA,
    Kind,
    SettingSpec,
)
from meetup_bot.telegram.errors_text import describe_error
from meetup_bot.texts.defaults import (
    ALLOWED_PLACEHOLDERS,
    DEFAULT_TEXTS,
    MESSAGE_TEXT_KEYS,
)
from meetup_bot.texts.render import WEEKDAYS_SHORT, clip

_PARSE = "HTML"
_TEXT_KEYS = tuple(DEFAULT_TEXTS)
_OK_MARK = "✅ "
_WARNING_MARK = "⚠️ "
_WEEKDAYS_PER_ROW = 4
# Окно показывает шаблон и предпросмотр; целиком длинный текст не влез бы в 4096 символов
PREVIEW_CLIP = 1200


# ---- общие помощники ------------------------------------------------------------------


def _set_notice(manager: DialogManager, text: str, *, ok: bool) -> None:
    set_notice(manager, (_OK_MARK if ok else _WARNING_MARK) + html.escape(text))


async def _guard(event: CallbackQuery | Message, manager: DialogManager) -> bool:
    """Права перепроверяются на каждом действии; без них диалог закрывается."""
    return await guard(event, manager, close=True)


async def _save(
    event: CallbackQuery | Message, manager: DialogManager, key: str, value: Any
) -> None:
    """Сохраняет значение через сервис; результат (или ошибку) кладёт в notice."""
    if not await _guard(event, manager):
        return
    try:
        warnings = await get_services(manager).settings.set(key, value, actor_id(manager))
    except DomainError as exc:
        _set_notice(manager, describe_error(exc), ok=False)
        return
    # Предупреждения не ошибка: значение уже сохранено
    set_notice(
        manager,
        "\n".join([f"{_OK_MARK}Сохранено.", *(_WARNING_MARK + html.escape(w) for w in warnings)]),
    )


async def _reset(event: CallbackQuery | Message, manager: DialogManager, key: str) -> None:
    if not await _guard(event, manager):
        return
    try:
        await get_services(manager).settings.reset(key, actor_id(manager))
    except DomainError as exc:
        _set_notice(manager, describe_error(exc), ok=False)
        return
    _set_notice(manager, "Возвращено значение по умолчанию.", ok=True)


def _current_key(manager: DialogManager) -> str:
    return str(manager.dialog_data["key"])


# ---- меню и списки групп --------------------------------------------------------------


def _open_group(group: str) -> Any:
    async def handler(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
        if not await _guard(callback, manager):
            return
        manager.dialog_data["group"] = group
        await manager.switch_to(SettingsSG.group)

    return handler


async def group_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    group = str(dialog_manager.dialog_data.get("group", GROUP_DEFAULTS))
    title, hint, keys = GROUPS[group]
    settings = get_services(dialog_manager).settings
    items = []
    for key in keys:
        value = await settings.get(key)
        items.append((button_label(key, value, await settings.is_default(key)), key))
    body = f"<b>{html.escape(title)}</b>"
    if hint:
        body += f"\n{html.escape(hint)}"
    body += "\n\nВыберите настройку, чтобы изменить."
    return {"body": with_notice(pop_notice(dialog_manager), [body]), "items": items}


async def _pick_setting(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    if not await _guard(callback, manager):
        return
    if item_id not in SCHEMA:
        return
    manager.dialog_data["key"] = item_id
    await manager.switch_to(SettingsSG.edit)


# ---- окно редактирования одной настройки ----------------------------------------------


async def edit_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    key = _current_key(dialog_manager)
    spec = SCHEMA[key]
    settings = get_services(dialog_manager).settings
    value = await settings.get(key)
    is_default = await settings.is_default(key)
    notice = pop_notice(dialog_manager)
    weekdays = [
        ((_OK_MARK if value == i else "") + name.lower(), i)
        for i, name in enumerate(WEEKDAYS_SHORT)
    ]
    return {
        "body": edit_body(spec, value, is_default, notice),
        "is_bool": spec.kind is Kind.BOOL,
        "is_weekday": key in WEEKDAY_KEYS,
        "can_clear": can_clear(spec),
        "is_default": is_default,
        "toggle_label": "Выключить" if value is True else "Включить",
        "weekdays": weekdays,
    }


async def _on_toggle(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    key = _current_key(manager)
    current = await get_services(manager).settings.get(key)
    await _save(callback, manager, key, not bool(current))


async def _on_weekday(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: int
) -> None:
    await _save(callback, manager, _current_key(manager), item_id)


async def _on_clear(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    key = _current_key(manager)
    # Необязательная настройка очищается в None, строковые — в пустую строку
    await _save(callback, manager, key, None if SCHEMA[key].kind is Kind.OPT_STR else "")


async def _on_reset(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _reset(callback, manager, _current_key(manager))


def _parse_input(spec: SettingSpec, message: Message) -> tuple[bool, Any]:
    """Достаёт значение из сообщения; (False, текст ошибки), если тип ввода не подходит."""
    if spec.key == PHOTO_KEY:
        if not message.photo:
            return False, "Отправьте именно фото (не файл и не текст)."
        return True, message.photo[-1].file_id
    if not message.text:
        return False, "Отправьте текстовое сообщение."
    text = message.text
    if spec.kind is Kind.INT:
        try:
            return True, int(text.strip())
        except ValueError:
            return False, f"Нужно целое число от {spec.min} до {spec.max}."
    if spec.kind is Kind.HTML:
        # html_text сохраняет форматирование, которое админ задал в Telegram
        return True, message.html_text
    return True, text


async def _on_input(message: Message, widget: Any, manager: DialogManager) -> None:
    key = _current_key(manager)
    spec = SCHEMA[key]
    if not await _guard(message, manager):
        return
    if not accepts_input(spec):
        _set_notice(manager, "Для этой настройки используйте кнопки.", ok=False)
        return
    ok, value = _parse_input(spec, message)
    if not ok:
        _set_notice(manager, value, ok=False)
        return
    await _save(message, manager, key, value)


_BACK_TO_GROUP = SwitchTo(Const(BACK), id="edit_back", state=SettingsSG.group)

edit_window = Window(
    Format("{body}"),
    Button(Format("{toggle_label}"), id="toggle", on_click=_on_toggle, when="is_bool"),
    Group(
        Select(
            Format("{item[0]}"),
            id="weekday",
            item_id_getter=lambda item: item[1],
            items="weekdays",
            type_factory=int,
            on_click=_on_weekday,
        ),
        width=_WEEKDAYS_PER_ROW,
        when="is_weekday",
    ),
    Button(Const("🧹 Очистить"), id="clear", on_click=_on_clear, when="can_clear"),
    Button(Const(RESET_DEFAULT), id="reset", on_click=_on_reset),
    _BACK_TO_GROUP,
    MessageInput(_on_input, content_types=[ContentType.TEXT, ContentType.PHOTO]),
    state=SettingsSG.edit,
    getter=edit_getter,
    parse_mode=_PARSE,
)


# ---- тексты сообщений -----------------------------------------------------------------


async def texts_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    settings = get_services(dialog_manager).settings
    items = []
    for key in _TEXT_KEYS:
        mark = "" if await settings.is_default(key) else " ✏️"
        items.append((SCHEMA[key].description + mark, key))
    body = "<b>Тексты сообщений</b>\nВыберите, что именно меняем. Значок ✏️ — текст уже изменён."
    return {"body": with_notice(pop_notice(dialog_manager), [body]), "items": items}


async def _pick_text(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    if not await _guard(callback, manager):
        return
    if item_id not in DEFAULT_TEXTS:
        return
    manager.dialog_data["key"] = item_id
    await manager.switch_to(SettingsSG.text_edit)


def _placeholders(template: str) -> list[str]:
    names = []
    for _literal, name, _spec, _conv in string.Formatter().parse(template):
        if name and "{" + name + "}" not in names:
            names.append("{" + name + "}")
    return names


async def text_edit_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    key = _current_key(dialog_manager)
    settings = get_services(dialog_manager).settings
    template = str(await settings.get(key))
    is_default = await settings.is_default(key)
    is_message = key in MESSAGE_TEXT_KEYS
    # Предпросмотр экранируем целиком: админ мог вставить в сообщение теги, а окно у нас
    # тоже HTML — сломанная разметка не должна ронять само меню
    preview = settings.render_text_now(key, escape_html=False, **SAMPLE_VARS)
    limit = (
        "без лимита (уходит обычным сообщением, форматирование сохраняется)"
        if is_message
        else f"до {POPUP_MAX_LEN} символов (всплывающее окно Telegram)"
    )
    used = ", ".join(_placeholders(DEFAULT_TEXTS[key])) or "нет"
    allowed = ", ".join("{" + n + "}" for n in sorted(ALLOWED_PLACEHOLDERS))
    mark = " (по умолчанию)" if is_default else ""
    lines = [
        f"<b>{html.escape(SCHEMA[key].description)}</b>",
        "",
        f"Текст сейчас{mark}:",
        f"<blockquote>{html.escape(clip(template, PREVIEW_CLIP))}</blockquote>",
        "",
        f"Подстановки в стандартном тексте: {html.escape(used)}",
        f"Все допустимые: {html.escape(allowed)}",
        "Фигурные скобки зарезервированы под подстановки.",
        f"Длина: {limit}.",
        "",
        "Предпросмотр с примерными значениями:",
        f"<blockquote>{html.escape(clip(preview, PREVIEW_CLIP))}</blockquote>",
        "",
        "Отправьте новый текст сообщением.",
    ]
    return {"body": with_notice(pop_notice(dialog_manager), lines)}


async def _on_text_input(message: Message, widget: Any, manager: DialogManager) -> None:
    key = _current_key(manager)
    if not await _guard(message, manager):
        return
    if not message.text:
        _set_notice(manager, "Отправьте текстовое сообщение.", ok=False)
        return
    # Для сообщений сохраняем форматирование (html_text); всплывающие окна HTML не понимают
    value = message.html_text if key in MESSAGE_TEXT_KEYS else message.text
    await _save(message, manager, key, value)


async def _on_text_reset(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _reset(callback, manager, _current_key(manager))


texts_window = Window(
    Format("{body}"),
    Group(
        Select(
            Format("{item[0]}"),
            id="text_pick",
            item_id_getter=lambda item: item[1],
            items="items",
            on_click=_pick_text,
        ),
        width=1,
    ),
    SwitchTo(Const(BACK), id="texts_back", state=SettingsSG.menu),
    state=SettingsSG.texts,
    getter=texts_getter,
    parse_mode=_PARSE,
)

text_edit_window = Window(
    Format("{body}"),
    Button(Const(RESET_DEFAULT), id="text_reset", on_click=_on_text_reset),
    SwitchTo(Const(BACK), id="text_edit_back", state=SettingsSG.texts),
    MessageInput(_on_text_input, content_types=[ContentType.TEXT]),
    state=SettingsSG.text_edit,
    getter=text_edit_getter,
    parse_mode=_PARSE,
)


# ---- часовой пояс ---------------------------------------------------------------------


async def timezone_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    settings = get_services(dialog_manager).settings
    current = str(await settings.get(TIMEZONE_KEY))
    mark = " (по умолчанию)" if await settings.is_default(TIMEZONE_KEY) else ""
    lines = [
        f"<b>{html.escape(label_of(TIMEZONE_KEY))}</b>",
        f"Сейчас: <b>{html.escape(current)}</b>{mark}",
        "",
        "Выберите из списка или отправьте название в формате IANA, например America/Montevideo.",
    ]
    zones = [((_OK_MARK if z == current else "") + z, z) for z in POPULAR_TIMEZONES]
    return {"body": with_notice(pop_notice(dialog_manager), lines), "zones": zones}


async def _on_zone(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    await _save(callback, manager, TIMEZONE_KEY, item_id)


async def _on_zone_input(message: Message, widget: Any, manager: DialogManager) -> None:
    if not message.text:
        _set_notice(manager, "Отправьте название часового пояса текстом.", ok=False)
        return
    await _save(message, manager, TIMEZONE_KEY, message.text)


async def _on_zone_reset(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _reset(callback, manager, TIMEZONE_KEY)


timezone_window = Window(
    Format("{body}"),
    Group(
        Select(
            Format("{item[0]}"),
            id="zone_pick",
            item_id_getter=lambda item: item[1],
            items="zones",
            on_click=_on_zone,
        ),
        width=1,
    ),
    Button(Const(RESET_DEFAULT), id="zone_reset", on_click=_on_zone_reset),
    SwitchTo(Const(BACK), id="zone_back", state=SettingsSG.menu),
    MessageInput(_on_zone_input, content_types=[ContentType.TEXT]),
    state=SettingsSG.timezone,
    getter=timezone_getter,
    parse_mode=_PARSE,
)


# ---- корневое меню и окно группы ------------------------------------------------------


async def menu_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    body = "<b>⚙️ Настройки</b>\nВыберите раздел."
    return {"body": with_notice(pop_notice(dialog_manager), [body])}


async def _open_texts(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    await manager.switch_to(SettingsSG.texts)


async def _open_timezone(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await _guard(callback, manager):
        return
    await manager.switch_to(SettingsSG.timezone)


menu_window = Window(
    Format("{body}"),
    Button(Const("Встречи по умолчанию"), id="g_defaults", on_click=_open_group(GROUP_DEFAULTS)),
    Button(Const("Расписание"), id="g_schedule", on_click=_open_group(GROUP_SCHEDULE)),
    Button(Const("Правила"), id="g_rules", on_click=_open_group(GROUP_RULES)),
    Button(Const("Тексты сообщений"), id="g_texts", on_click=_open_texts),
    Button(Const("Часовой пояс"), id="g_timezone", on_click=_open_timezone),
    Cancel(Const(TO_MENU)),
    state=SettingsSG.menu,
    getter=menu_getter,
    parse_mode=_PARSE,
)

group_window = Window(
    Format("{body}"),
    Group(
        Select(
            Format("{item[0]}"),
            id="setting_pick",
            item_id_getter=lambda item: item[1],
            items="items",
            on_click=_pick_setting,
        ),
        width=1,
    ),
    SwitchTo(Const(BACK), id="group_back", state=SettingsSG.menu),
    state=SettingsSG.group,
    getter=group_getter,
    parse_mode=_PARSE,
)

DIALOGS: list[Dialog] = [
    Dialog(menu_window, group_window, edit_window, texts_window, text_edit_window, timezone_window)
]
