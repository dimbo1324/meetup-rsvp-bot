"""Схема настроек (раздел 7): тип, значение по умолчанию, ограничения, описание.

Значения в БД хранятся в JSON-виде: время — строкой "HH:MM", остальное — как есть.
Здесь же чистая валидация; сервис настроек только применяет её и пишет аудит.
"""

from __future__ import annotations

import enum
import string
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from meetup_bot.services.errors import DomainError
from meetup_bot.texts.defaults import ALLOWED_PLACEHOLDERS, DEFAULT_TEXTS, TEXT_PREFIX

POPUP_MAX_LEN = 200
PLACE_MAX_LEN = 200
# Подпись поста с фото ≤ 1024; описание не должно съедать всё место автоблока (5.10)
DESCRIPTION_WARN_LEN = 700
DESCRIPTION_MAX_LEN = 3500

# Тестовые значения для пробного рендера текстов
SAMPLE_VARS: dict[str, str] = {
    "date": "25.10",
    "time": "17:00",
    "weekday": "суббота",
    "place": "Кафе",
    "deadline": "пятница, 20:00",
    "position": "1",
    "name": "Имя",
    "reason": "Причина",
}

# Теги, которые принимает Telegram в parse_mode=HTML
_TG_TAGS = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
     "tg-spoiler", "span", "blockquote", "tg-emoji"}
)  # fmt: skip


class SettingsValidationError(DomainError):
    """Значение настройки не прошло проверку; сообщение можно показывать админу."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key
        self.message = message


class Kind(enum.StrEnum):
    INT = "int"
    BOOL = "bool"
    STR = "str"
    TIME = "time"
    URL = "url"
    HTML = "html"
    TIMEZONE = "timezone"
    OPT_STR = "opt_str"
    TEXT = "text"


@dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: Kind
    default: Any
    description: str
    min: int | None = None
    max: int | None = None
    max_len: int | None = None


def _int(key: str, default: int, lo: int, hi: int, description: str) -> SettingSpec:
    return SettingSpec(key, Kind.INT, default, description, min=lo, max=hi)


def _bool(key: str, default: bool, description: str) -> SettingSpec:
    return SettingSpec(key, Kind.BOOL, default, description)


_TEXT_DESCRIPTIONS: dict[str, str] = {
    "text.registered": "Успешная запись",
    "text.registered_late": "Запись после дедлайна отмены",
    "text.full": "Мест нет",
    "text.waitlisted": "Встал в лист ожидания",
    "text.already": "Повторное нажатие «Записаться»",
    "text.already_waitlist": "Повторное нажатие в листе ожидания",
    "text.only_one_seat": "«+1» при одном свободном месте",
    "text.closed": "Запись закрыта",
    "text.banned": "Человек в бане",
    "text.cancelled": "Успешная отмена",
    "text.cancel_too_late": "Отмена после дедлайна",
    "text.not_registered": "Отмена без записи",
    "text.enable_dm_hint": "Подсказка, если у бота нет личного чата с человеком",
    "text.reminder": "Напоминание в личку",
    "text.promoted": "Продвижение из листа ожидания",
    "text.event_cancelled": "Встреча отменена",
    "text.event_moved": "Встреча перенесена",
}

_SPECS: tuple[SettingSpec, ...] = (
    _int("capacity", 10, 1, 200, "Лимит мест для новых встреч"),
    _int("event_weekday", 5, 0, 6, "День встречи (пн = 0, сб = 5)"),
    SettingSpec("event_time", Kind.TIME, "17:00", "Время начала встречи"),
    SettingSpec("default_place", Kind.STR, "", "Место по умолчанию", max_len=PLACE_MAX_LEN),
    SettingSpec("default_place_url", Kind.URL, "", "Ссылка на карту (https://)"),
    SettingSpec("default_description", Kind.HTML, "", "Текст поста по умолчанию (HTML)"),
    SettingSpec("default_photo_file_id", Kind.OPT_STR, None, "Фото по умолчанию (file_id)"),
    _bool("auto_create_enabled", True, "Автосоздание встреч"),
    _int("auto_create_weekday", 6, 0, 6, "День недели автосоздания (пн = 0)"),
    SettingSpec("auto_create_time", Kind.TIME, "10:00", "Время автосоздания"),
    _int("cancel_deadline_days_before", 1, 0, 7, "Дедлайн отмены: за сколько дней"),
    SettingSpec("cancel_deadline_time", Kind.TIME, "20:00", "Дедлайн отмены: во сколько"),
    _int("registration_close_hours_before", 3, 0, 72, "Закрытие записи, часов до начала"),
    _bool("reminder_enabled", True, "Напоминания в личку"),
    _int("reminder_hours_before", 24, 1, 72, "За сколько часов напоминать"),
    _int("attendance_prompt_hours_after", 5, 1, 48, "Через сколько часов просить отметить явку"),
    _int("ban_after_no_shows", 1, 0, 10, "Порог автобана по неявкам, 0 = выключен"),
    _bool("remove_banned_from_future_events", True, "Снимать забаненного с будущих встреч"),
    _bool("notify_user_on_ban", False, "Сообщать человеку о бане"),
    _bool("show_names_in_post", True, "Имена в посте (иначе только счётчик)"),
    _bool("plus_one_enabled", False, "Кнопка «+1»"),
    _bool("waitlist_enabled", False, "Лист ожидания"),
    _int("late_promotion_hours", 24, 0, 72, "Порог «поздней» подвижки из листа ожидания, часов"),
    _bool("notify_admins_on_full", True, "Уведомлять админов о заполнении встречи"),
    _bool("notify_admins_on_cancel", True, "Уведомлять админов об отменах"),
    SettingSpec("timezone", Kind.TIMEZONE, "America/Montevideo", "Часовой пояс (IANA)"),
    *(
        SettingSpec(key, Kind.TEXT, text, _TEXT_DESCRIPTIONS[key], max_len=POPUP_MAX_LEN)
        for key, text in DEFAULT_TEXTS.items()
    ),
)

SCHEMA: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}


def get_spec(key: str) -> SettingSpec:
    try:
        return SCHEMA[key]
    except KeyError:
        raise SettingsValidationError(key, f"Неизвестная настройка «{key}».") from None


def parse_hhmm(value: str) -> tuple[int, int] | None:
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) == 2 for p in parts):
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        return None
    return hours, minutes


def check_template(key: str, template: str) -> None:
    """Рендерит шаблон тестовыми значениями; неизвестные подстановки и форматы отклоняются."""
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise SettingsValidationError(key, f"Некорректные фигурные скобки: {exc}.") from exc
    for _literal, name, spec, conversion in fields:
        if name is None:
            continue
        if name not in ALLOWED_PLACEHOLDERS or spec or conversion:
            allowed = ", ".join("{" + n + "}" for n in sorted(ALLOWED_PLACEHOLDERS))
            raise SettingsValidationError(
                key, f"Неизвестная подстановка «{{{name}}}». Доступны: {allowed}."
            )
    template.format(**SAMPLE_VARS)


class _TagChecker(HTMLParser):
    """Проверяет, что HTML состоит из тегов Telegram и теги закрыты в правильном порядке."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.error: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _TG_TAGS:
            self.error = f"тег <{tag}> не поддерживается Telegram"
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.error = f"тег </{tag}> закрыт не к месту"
        else:
            self.stack.pop()


def check_html(key: str, value: str) -> list[str]:
    """Проверка HTML описания; возвращает предупреждения, ошибки бросает исключением."""
    if len(value) > DESCRIPTION_MAX_LEN:
        raise SettingsValidationError(
            key, f"Описание слишком длинное: {len(value)} из {DESCRIPTION_MAX_LEN} символов."
        )
    checker = _TagChecker()
    checker.feed(value)
    checker.close()
    if checker.error is None and checker.stack:
        checker.error = f"тег <{checker.stack[-1]}> не закрыт"
    if checker.error:
        raise SettingsValidationError(key, f"Некорректный HTML: {checker.error}.")
    if len(value) > DESCRIPTION_WARN_LEN:
        return [
            f"Описание длинное ({len(value)} символов): в посте с фото оно может быть обрезано."
        ]
    return []


def validate(spec: SettingSpec, value: Any) -> tuple[Any, list[str]]:
    """Проверяет значение и возвращает (нормализованное JSON-значение, предупреждения)."""
    key = spec.key
    warnings: list[str] = []
    kind = spec.kind

    if kind is Kind.INT:
        # bool — подкласс int, но «True» как лимит мест — явная ошибка ввода
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError(key, "Нужно целое число.")
        assert spec.min is not None and spec.max is not None
        if not spec.min <= value <= spec.max:
            raise SettingsValidationError(key, f"Значение должно быть от {spec.min} до {spec.max}.")
        return value, warnings
    if kind is Kind.BOOL:
        if not isinstance(value, bool):
            raise SettingsValidationError(key, "Нужно значение «вкл» или «выкл».")
        return value, warnings

    if kind is Kind.OPT_STR:
        if value is None:
            return None, warnings
        if not isinstance(value, str) or not value.strip():
            raise SettingsValidationError(key, "Нужна непустая строка или пустое значение.")
        return value.strip(), warnings

    if not isinstance(value, str):
        raise SettingsValidationError(key, "Нужна строка.")

    if kind is Kind.TIME:
        value = value.strip()
        if parse_hhmm(value) is None:
            raise SettingsValidationError(key, "Время должно быть в формате ЧЧ:ММ, например 17:00.")
        return value, warnings
    if kind is Kind.URL:
        value = value.strip()
        if value and (not value.startswith("https://") or len(value) <= len("https://")):
            raise SettingsValidationError(key, "Ссылка должна начинаться с https://")
        if any(ch.isspace() for ch in value):
            raise SettingsValidationError(key, "В ссылке не должно быть пробелов.")
        return value, warnings
    if kind is Kind.TIMEZONE:
        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise SettingsValidationError(
                key, "Неизвестный часовой пояс. Пример: America/Montevideo."
            ) from exc
        return value, warnings
    if kind is Kind.HTML:
        return value, check_html(key, value)
    if kind is Kind.TEXT:
        if not value.strip():
            raise SettingsValidationError(key, "Текст не может быть пустым.")
        check_template(key, value)
        rendered = value.format(**SAMPLE_VARS)
        assert spec.max_len is not None
        if len(rendered) > spec.max_len:
            raise SettingsValidationError(
                key,
                f"Текст слишком длинный: {len(rendered)} из {spec.max_len} символов "
                "(лимит всплывающего окна Telegram).",
            )
        return value, warnings

    # Kind.STR
    if spec.max_len is not None and len(value) > spec.max_len:
        raise SettingsValidationError(key, f"Не длиннее {spec.max_len} символов.")
    return value, warnings


__all__ = [
    "SAMPLE_VARS",
    "SCHEMA",
    "TEXT_PREFIX",
    "Kind",
    "SettingSpec",
    "SettingsValidationError",
    "get_spec",
    "parse_hhmm",
    "validate",
]
