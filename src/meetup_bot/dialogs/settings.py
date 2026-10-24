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

from meetup_bot.dialogs._common import NO_ACCESS, actor_id, describe_error, get_services, is_admin
from meetup_bot.dialogs.settings_view import (
    GROUPS,
    PHOTO_KEY,
    POPULAR_TIMEZONES,
    WEEKDAY_KEYS,
    WEEKDAYS_SHORT,
    accepts_input,
    button_label,
    can_clear,
    edit_body,
    label_of,
    with_notice,
)
from meetup_bot.dialogs.states import SettingsSG
from meetup_bot.services.settings_schema import (
    POPUP_MAX_LEN,
    SAMPLE_VARS,
    SCHEMA,
    Kind,
    SettingSpec,
)
from meetup_bot.texts.defaults import (
    ALLOWED_PLACEHOLDERS,
    DEFAULT_TEXTS,
    MESSAGE_TEXT_KEYS,
)

_PARSE = "HTML"
_TEXT_KEYS = tuple(DEFAULT_TEXTS)


# ---- общие помощники ------------------------------------------------------------------


def _set_notice(manager: DialogManager, text: str, *, ok: bool) -> None:
    manager.dialog_data["notice"] = ("✅ " if ok else "⚠️ ") + html.escape(text)


def _pop_notice(manager: DialogManager) -> str:
    notice = manager.dialog_data.pop("notice", "")
    return str(notice)


async def _deny(manager: DialogManager) -> None:
    _set_notice(manager, NO_ACCESS, ok=False)
    await manager.done()


async def _save(manager: DialogManager, key: str, value: Any) -> None:
    """Сохраняет значение через сервис; результат (или ошибку) кладёт в notice."""
    if not await is_admin(manager):
        await _deny(manager)
        return
    try:
        warnings = await get_services(manager).settings.set(key, value, actor_id(manager))
    except Exception as exc:
        _set_notice(manager, describe_error(exc), ok=False)
        return
    # Предупреждения не ошибка: значение уже сохранено
    manager.dialog_data["notice"] = "\n".join(
        ["✅ Сохранено.", *("⚠️ " + html.escape(w) for w in warnings)]
    )


async def _reset(manager: DialogManager, key: str) -> None:
    if not await is_admin(manager):
        await _deny(manager)
        return
    try:
        await get_services(manager).settings.reset(key, actor_id(manager))
    except Exception as exc:
        _set_notice(manager, describe_error(exc), ok=False)
        return
    _set_notice(manager, "Возвращено значение по умолчанию.", ok=True)


def _current_key(manager: DialogManager) -> str:
    return str(manager.dialog_data["key"])


# ---- меню и списки групп --------------------------------------------------------------


def _open_group(group: str) -> Any:
    async def handler(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
        if not await is_admin(manager):
            await _deny(manager)
            return
        manager.dialog_data["group"] = group
        await manager.switch_to(SettingsSG.group)

    return handler


async def group_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    group = str(dialog_manager.dialog_data.get("group", "defaults"))
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
    return {"body": with_notice(_pop_notice(dialog_manager), [body]), "items": items}


async def _pick_setting(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    if not await is_admin(manager):
        await _deny(manager)
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
    notice = _pop_notice(dialog_manager)
    weekdays = [(("✅ " if value == i else "") + name, i) for i, name in enumerate(WEEKDAYS_SHORT)]
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
    await _save(manager, key, not bool(current))


async def _on_weekday(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: int
) -> None:
    await _save(manager, _current_key(manager), item_id)


async def _on_clear(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    key = _current_key(manager)
    # Необязательная настройка очищается в None, строковые — в пустую строку
    await _save(manager, key, None if SCHEMA[key].kind is Kind.OPT_STR else "")


async def _on_reset(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _reset(manager, _current_key(manager))


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
    if not await is_admin(manager):
        await _deny(manager)
        return
    if not accepts_input(spec):
        _set_notice(manager, "Для этой настройки используйте кнопки.", ok=False)
        return
    ok, value = _parse_input(spec, message)
    if not ok:
        _set_notice(manager, value, ok=False)
        return
    await _save(manager, key, value)


_BACK_TO_GROUP = SwitchTo(Const("⬅️ Назад"), id="edit_back", state=SettingsSG.group)

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
        width=4,
        when="is_weekday",
    ),
    Button(Const("🧹 Очистить"), id="clear", on_click=_on_clear, when="can_clear"),
    Button(Const("↩️ Сбросить по умолчанию"), id="reset", on_click=_on_reset),
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
    return {"body": with_notice(_pop_notice(dialog_manager), [body]), "items": items}


async def _pick_text(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    if not await is_admin(manager):
        await _deny(manager)
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


# Окно показывает шаблон и предпросмотр; целиком длинный текст не влез бы в 4096 символов
PREVIEW_CLIP = 1200


def _clip(text: str) -> str:
    return text if len(text) <= PREVIEW_CLIP else text[: PREVIEW_CLIP - 1] + "…"


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
        f"<blockquote>{html.escape(_clip(template))}</blockquote>",
        "",
        f"Подстановки в стандартном тексте: {html.escape(used)}",
        f"Все допустимые: {html.escape(allowed)}",
        "Фигурные скобки зарезервированы под подстановки.",
        f"Длина: {limit}.",
        "",
        "Предпросмотр с примерными значениями:",
        f"<blockquote>{html.escape(_clip(preview))}</blockquote>",
        "",
        "Отправьте новый текст сообщением.",
    ]
    return {"body": with_notice(_pop_notice(dialog_manager), lines)}


async def _on_text_input(message: Message, widget: Any, manager: DialogManager) -> None:
    key = _current_key(manager)
    if not await is_admin(manager):
        await _deny(manager)
        return
    if not message.text:
        _set_notice(manager, "Отправьте текстовое сообщение.", ok=False)
        return
    # Для сообщений сохраняем форматирование (html_text); всплывающие окна HTML не понимают
    value = message.html_text if key in MESSAGE_TEXT_KEYS else message.text
    await _save(manager, key, value)


async def _on_text_reset(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _reset(manager, _current_key(manager))


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
    SwitchTo(Const("⬅️ Назад"), id="texts_back", state=SettingsSG.menu),
    state=SettingsSG.texts,
    getter=texts_getter,
    parse_mode=_PARSE,
)

text_edit_window = Window(
    Format("{body}"),
    Button(Const("↩️ Сбросить по умолчанию"), id="text_reset", on_click=_on_text_reset),
    SwitchTo(Const("⬅️ Назад"), id="text_edit_back", state=SettingsSG.texts),
    MessageInput(_on_text_input, content_types=[ContentType.TEXT]),
    state=SettingsSG.text_edit,
    getter=text_edit_getter,
    parse_mode=_PARSE,
)


# ---- часовой пояс ---------------------------------------------------------------------


async def timezone_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    settings = get_services(dialog_manager).settings
    current = str(await settings.get("timezone"))
    mark = " (по умолчанию)" if await settings.is_default("timezone") else ""
    lines = [
        f"<b>{html.escape(label_of('timezone'))}</b>",
        f"Сейчас: <b>{html.escape(current)}</b>{mark}",
        "",
        "Выберите из списка или отправьте название в формате IANA, например America/Montevideo.",
    ]
    zones = [(("✅ " if z == current else "") + z, z) for z in POPULAR_TIMEZONES]
    return {"body": with_notice(_pop_notice(dialog_manager), lines), "zones": zones}


async def _on_zone(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: str
) -> None:
    await _save(manager, "timezone", item_id)


async def _on_zone_input(message: Message, widget: Any, manager: DialogManager) -> None:
    if not message.text:
        _set_notice(manager, "Отправьте название часового пояса текстом.", ok=False)
        return
    await _save(manager, "timezone", message.text)


async def _on_zone_reset(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _reset(manager, "timezone")


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
    Button(Const("↩️ Сбросить по умолчанию"), id="zone_reset", on_click=_on_zone_reset),
    SwitchTo(Const("⬅️ Назад"), id="zone_back", state=SettingsSG.menu),
    MessageInput(_on_zone_input, content_types=[ContentType.TEXT]),
    state=SettingsSG.timezone,
    getter=timezone_getter,
    parse_mode=_PARSE,
)


# ---- корневое меню и окно группы ------------------------------------------------------


async def menu_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    body = "<b>⚙️ Настройки</b>\nВыберите раздел."
    return {"body": with_notice(_pop_notice(dialog_manager), [body])}


async def _open_texts(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await is_admin(manager):
        await _deny(manager)
        return
    await manager.switch_to(SettingsSG.texts)


async def _open_timezone(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await is_admin(manager):
        await _deny(manager)
        return
    await manager.switch_to(SettingsSG.timezone)


menu_window = Window(
    Format("{body}"),
    Button(Const("Встречи по умолчанию"), id="g_defaults", on_click=_open_group("defaults")),
    Button(Const("Расписание"), id="g_schedule", on_click=_open_group("schedule")),
    Button(Const("Правила"), id="g_rules", on_click=_open_group("rules")),
    Button(Const("Тексты сообщений"), id="g_texts", on_click=_open_texts),
    Button(Const("Часовой пояс"), id="g_timezone", on_click=_open_timezone),
    Cancel(Const("⬅️ В меню")),
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
    SwitchTo(Const("⬅️ Назад"), id="group_back", state=SettingsSG.menu),
    state=SettingsSG.group,
    getter=group_getter,
    parse_mode=_PARSE,
)

DIALOGS: list[Dialog] = [
    Dialog(menu_window, group_window, edit_window, texts_window, text_edit_window, timezone_window)
]
