"""Подписи, форматирование значений и тексты окон настроек (без aiogram-dialog)."""

from __future__ import annotations

import html
from typing import Any

from meetup_bot.services.defaults import DEFAULT_TIMEZONE
from meetup_bot.services.settings_schema import SCHEMA, Kind, SettingSpec
from meetup_bot.texts.render import WEEKDAYS, clip

WEEKDAY_KEYS = frozenset({"event_weekday"})
PHOTO_KEY = "default_photo_file_id"
TIMEZONE_KEY = "timezone"

GROUP_DEFAULTS = "defaults"
GROUP_SCHEDULE = "schedule"
GROUP_RULES = "rules"

POPULAR_TIMEZONES = (
    DEFAULT_TIMEZONE,
    "America/Argentina/Buenos_Aires",
    "Europe/Moscow",
    "Europe/Madrid",
    "UTC",
)

# Группа -> (заголовок, подсказка, ключи в порядке показа)
GROUPS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    GROUP_DEFAULTS: (
        "Встречи по умолчанию",
        "Эти значения применяются к НОВЫМ встречам; уже созданные не меняются.",
        (
            "capacity",
            "event_weekday",
            "event_time",
            "event_duration_minutes",
            "default_place",
            "default_place_url",
            "default_description",
            "default_photo_file_id",
        ),
    ),
    GROUP_SCHEDULE: (
        "Расписание",
        "Изменения влияют на НОВЫЕ встречи; уже созданные остаются как были.",
        (
            "auto_create_enabled",
            "auto_create_days_after",
            "auto_create_time",
            "cancel_deadline_days_before",
            "cancel_deadline_time",
            "registration_close_minutes_after_start",
            "reminder_enabled",
            "reminder_hours_before",
            "attendance_prompt_minutes_after",
            "post_reminder_days_before",
            "post_reminder_time",
        ),
    ),
    GROUP_RULES: (
        "Правила",
        "",
        (
            "ban_after_no_shows",
            "remove_banned_from_future_events",
            "notify_user_on_ban",
            "show_names_in_post",
            "plus_one_enabled",
            "waitlist_enabled",
            "late_promotion_hours",
            "notify_admins_on_full",
            "notify_admins_on_cancel",
        ),
    ),
}

LABELS: dict[str, str] = {
    "capacity": "Лимит мест",
    "event_weekday": "День встречи",
    "event_time": "Время начала",
    "event_duration_minutes": "Длительность встречи",
    "default_place": "Место",
    "default_place_url": "Ссылка на карту",
    "default_description": "Описание в посте",
    "default_photo_file_id": "Фото в посте",
    "auto_create_enabled": "Автосоздание встреч",
    "auto_create_days_after": "Автосоздание: дней после встречи",
    "auto_create_time": "Время автосоздания",
    "cancel_deadline_days_before": "Дедлайн отмены: дней до встречи",
    "cancel_deadline_time": "Дедлайн отмены: время",
    "registration_close_minutes_after_start": "Закрытие записи",
    "reminder_enabled": "Напоминания в личку",
    "reminder_hours_before": "Когда напоминать",
    "attendance_prompt_minutes_after": "Запрос явки через",
    "post_reminder_days_before": "Напоминание разослать пост",
    "post_reminder_time": "Время напоминания о посте",
    "ban_after_no_shows": "Автобан по неявкам",
    "remove_banned_from_future_events": "Снимать забаненных со встреч",
    "notify_user_on_ban": "Сообщать о бане человеку",
    "show_names_in_post": "Имена в посте",
    "plus_one_enabled": "Кнопка «+1»",
    "waitlist_enabled": "Лист ожидания",
    "late_promotion_hours": "Порог «поздней» подвижки",
    "notify_admins_on_full": "Сообщать админам о заполнении",
    "notify_admins_on_cancel": "Сообщать админам об отменах",
    TIMEZONE_KEY: "Часовой пояс",
}

# Пояснения сверх краткого описания из схемы
EXTRA_HELP: dict[str, str] = {
    "ban_after_no_shows": "Сколько неявок приводит к бану. 0 — автобан выключен.",
    "late_promotion_hours": (
        "Если место освободилось позже чем за столько часов до встречи, "
        "человека из листа ожидания записывают как «поздно»."
    ),
    "registration_close_minutes_after_start": (
        "Через сколько минут после начала встречи запись закрывается: опоздавшие ещё могут "
        "записаться, если есть места. 0 — в момент начала. Закрыть раньше можно вручную "
        "в карточке встречи."
    ),
    "attendance_prompt_minutes_after": (
        "Через сколько минут после начала прислать админам список для отметки явки. "
        "Не раньше закрытия записи."
    ),
    "post_reminder_days_before": (
        "За сколько дней до встречи напомнить в служебном чате, что пост ещё не разослан. "
        "0 — не напоминать."
    ),
    "auto_create_days_after": (
        "Через сколько дней после встречи создавать следующую: "
        "0 — в день встречи, 1 — на следующий день."
    ),
    "cancel_deadline_days_before": "За сколько дней до встречи заканчивается приём отмен.",
    "show_names_in_post": "Выкл (по умолчанию) — в посте только счётчик и очередь, без имён.",
}

_UNITS: dict[str, str] = {
    "capacity": " мест",
    "cancel_deadline_days_before": " дн.",
    "auto_create_days_after": " дн. после встречи",
    "event_duration_minutes": " мин",
    "registration_close_minutes_after_start": " мин после начала",
    "post_reminder_days_before": " дн. до встречи",
    "reminder_hours_before": " ч до встречи",
    "attendance_prompt_minutes_after": " мин после начала",
    "late_promotion_hours": " ч",
}

# Числовые настройки, у которых 0 означает особый режим, а не «0 единиц»
_ZERO_VALUES: dict[str, str] = {
    "ban_after_no_shows": "выключен",
    "post_reminder_days_before": "выключено",
    "registration_close_minutes_after_start": "в момент начала",
}

_FREE_TEXT_KINDS = frozenset({Kind.STR, Kind.URL, Kind.HTML, Kind.OPT_STR})


def label_of(key: str) -> str:
    return LABELS.get(key, SCHEMA[key].description)


def accepts_input(spec: SettingSpec) -> bool:
    """Настройки, которые вводятся сообщением (остальные — только кнопками)."""
    return spec.kind is not Kind.BOOL and spec.key not in WEEKDAY_KEYS


def can_clear(spec: SettingSpec) -> bool:
    return spec.kind in _FREE_TEXT_KINDS


def display_value(key: str, value: Any, *, limit: int = 40) -> str:
    """Человеческое значение настройки (обычный текст, без экранирования)."""
    spec = SCHEMA[key]
    if spec.kind is Kind.BOOL:
        return "включено" if value else "выключено"
    if key in WEEKDAY_KEYS:
        return WEEKDAYS[int(value)]
    if key == PHOTO_KEY:
        return "задано" if value else "не задано"
    if spec.kind is Kind.INT and int(value) == 0 and key in _ZERO_VALUES:
        return _ZERO_VALUES[key]
    if key == "ban_after_no_shows":
        return f"порог {int(value)}"
    if spec.kind is Kind.INT:
        return f"{value}{_UNITS.get(key, '')}"
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        return "не задано"
    return clip(text, limit)


def button_label(key: str, value: Any, is_default: bool) -> str:
    mark = " · по умолч." if is_default else ""
    return f"{label_of(key)}: {display_value(key, value, limit=24)}{mark}"


def input_prompt(spec: SettingSpec) -> str:
    kind = spec.kind
    if spec.key in WEEKDAY_KEYS or kind is Kind.BOOL:
        return "Выберите значение кнопками."
    if spec.key == PHOTO_KEY:
        return "Отправьте фото — оно станет фото по умолчанию."
    if kind is Kind.INT:
        return f"Отправьте число от {spec.min} до {spec.max}."
    if kind is Kind.TIME:
        return "Отправьте время в формате ЧЧ:ММ, например 17:00."
    if kind is Kind.URL:
        return "Отправьте ссылку, начинающуюся с https://"
    if kind is Kind.HTML:
        return "Отправьте текст описания: жирный, курсив и ссылки сохранятся."
    limit = f" (до {spec.max_len} символов)" if spec.max_len else ""
    return f"Отправьте новый текст{limit}."


def with_notice(notice: str, lines: list[str]) -> str:
    """Склеивает строки окна; notice уже экранирован (HTML)."""
    if notice:
        lines = [notice, "", *lines]
    return "\n".join(lines)


def edit_body(spec: SettingSpec, value: Any, is_default: bool, notice: str) -> str:
    """HTML-текст окна редактирования одной настройки."""
    key = spec.key
    shown = display_value(key, value, limit=800)
    mark = " (по умолчанию)" if is_default else ""
    lines = [f"<b>{html.escape(label_of(key))}</b>", html.escape(spec.description)]
    if key in EXTRA_HELP:
        lines.append(html.escape(EXTRA_HELP[key]))
    lines += ["", f"Сейчас: <b>{html.escape(shown)}</b>{mark}", "", input_prompt(spec)]
    return with_notice(notice, lines)
