"""Схема настроек (раздел 7): тип, значение по умолчанию, ограничения, описание.

Значения в БД хранятся в JSON-виде: время — строкой "HH:MM", остальное — как есть.
Здесь же чистая валидация; сервис настроек только применяет её и пишет аудит.
"""

from __future__ import annotations

import enum
import re
import string
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from meetup_bot.services.errors import DomainError
from meetup_bot.texts.defaults import (
    ALLOWED_PLACEHOLDERS,
    DEFAULT_TEXTS,
    MESSAGE_TEXT_KEYS,
    TEXT_PREFIX,
)

POPUP_MAX_LEN = 200
# Текст-сообщение: шаблон до 3500 символов, а с худшими подстановками — в лимит Telegram 4096
MESSAGE_MAX_LEN = 3500
TELEGRAM_MESSAGE_LIMIT = 4096
PLACE_MAX_LEN = 200
# Подпись поста с фото ≤ 1024; описание не должно съедать всё место автоблока (5.10)
DESCRIPTION_WARN_LEN = 700
DESCRIPTION_MAX_LEN = 3500
URL_MAX_LEN = 2000
FILE_ID_MAX_LEN = 256
# Пределы значений, которые потребители подставляют в тексты (имя пользователя, причина)
NAME_MAX_LEN = 64
REASON_MAX_LEN = 200

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

# Худший случай для проверки длины всплывающих окон: длиннейшие реальные значения подстановок
WORST_CASE_VARS: dict[str, str] = {
    "date": "25.10.2026",
    "time": "17:00",
    "weekday": "воскресенье",
    "place": "М" * PLACE_MAX_LEN,
    "deadline": "воскресенье, 23:59",
    "position": "999",
    "name": "И" * NAME_MAX_LEN,
    "reason": "П" * REASON_MAX_LEN,
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
    "text.event_moved": "Сообщение о переносе встречи",
    "text.attendance_marked_by": "Подпись «явку отметил(а) …»",
    "text.ban_notice": "Сообщение человеку о бане (если включено notify_user_on_ban)",
}

_SPECS: tuple[SettingSpec, ...] = (
    _int("capacity", 10, 1, 200, "Лимит мест для новых встреч"),
    _int("event_weekday", 5, 0, 6, "День встречи (пн = 0, сб = 5)"),
    SettingSpec("event_time", Kind.TIME, "17:00", "Время начала встречи"),
    SettingSpec("default_place", Kind.STR, "", "Место по умолчанию", max_len=PLACE_MAX_LEN),
    SettingSpec(
        "default_place_url", Kind.URL, "", "Ссылка на карту (https://)", max_len=URL_MAX_LEN
    ),
    SettingSpec("default_description", Kind.HTML, "", "Текст поста по умолчанию (HTML)"),
    SettingSpec(
        "default_photo_file_id",
        Kind.OPT_STR,
        None,
        "Фото по умолчанию (file_id)",
        max_len=FILE_ID_MAX_LEN,
    ),
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
        SettingSpec(
            key,
            Kind.TEXT,
            text,
            _TEXT_DESCRIPTIONS[key],
            # Лимит 200 — только у всплывающих окон; у сообщений его нет
            max_len=MESSAGE_MAX_LEN if key in MESSAGE_TEXT_KEYS else POPUP_MAX_LEN,
        )
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


_HREF_HTTPS = re.compile(r"https://[^\s\"<>]+")
_HREF_USER = re.compile(r"tg://user\?id=\d+")
_LANG_CLASS = re.compile(r"language-[\w+#.-]+")
# Telegram понимает четыре именованные сущности и любые числовые
_ENTITY = re.compile(r"&(?:lt|gt|amp|quot|#\d{1,7}|#[xX][0-9a-fA-F]{1,6});")


def _check_attrs(tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
    """Белый список атрибутов: всё, что не перечислено, отклоняется (возвращает причину)."""
    names = [name for name, _ in attrs]
    if len(set(names)) != len(names):
        return f"повторяющийся атрибут в <{tag}>"
    pairs = {name: (value or "") for name, value in attrs}
    if tag == "a":
        href = pairs.get("href", "")
        ok_href = _HREF_HTTPS.fullmatch(href) or _HREF_USER.fullmatch(href)
        if set(pairs) != {"href"} or not ok_href:
            return "ссылка <a> допускает только href вида https://… или tg://user?id=…"
    elif tag == "span":
        if pairs != {"class": "tg-spoiler"}:
            return '<span> допускается только с class="tg-spoiler"'
    elif tag in {"code", "pre"}:
        if pairs and not (set(pairs) == {"class"} and _LANG_CLASS.fullmatch(pairs["class"])):
            return f'<{tag}> допускает только class="language-…"'
    elif tag == "tg-emoji":
        if set(pairs) != {"emoji-id"} or not pairs["emoji-id"].isdigit():
            return "<tg-emoji> требует числовой emoji-id"
    elif pairs:
        return f"тег <{tag}> не принимает атрибутов"
    return None


class _TagChecker(HTMLParser):
    """Проверяет, что HTML состоит из тегов Telegram и теги закрыты в правильном порядке.

    Валидатор строже парсера: Telegram отвечает ошибкой на «странный» HTML уже при
    публикации поста. Атрибуты — по белому списку, комментарии/декларации/инструкции и
    голые `<` запрещены (голые `&` ловит `check_html` по сырому тексту).
    """

    def __init__(self) -> None:
        # convert_charrefs=False: иначе парсер молча «чинит» сущности и «<» в тексте
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.error: str | None = None

    def fail(self, message: str) -> None:
        if self.error is None:
            self.error = message

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _TG_TAGS:
            self.fail(f"тег <{tag}> не поддерживается Telegram")
            return
        reason = _check_attrs(tag, attrs)
        if reason:
            self.fail(reason)
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.fail(f"тег </{tag}> закрыт не к месту")
        else:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if "<" in data:
            self.fail("неэкранированный символ «<» (используйте &lt;)")

    def handle_comment(self, data: str) -> None:
        self.fail("комментарии не поддерживаются")

    def handle_decl(self, decl: str) -> None:
        self.fail("декларации <!…> не поддерживаются")

    def handle_pi(self, data: str) -> None:
        self.fail("инструкции <?…> не поддерживаются")

    def unknown_decl(self, data: str) -> None:
        self.fail("декларации <![…]> не поддерживаются")


def check_html(key: str, value: str) -> list[str]:
    """Проверка HTML описания; возвращает предупреждения, ошибки бросает исключением."""
    if len(value) > DESCRIPTION_MAX_LEN:
        raise SettingsValidationError(
            key, f"Описание слишком длинное: {len(value)} из {DESCRIPTION_MAX_LEN} символов."
        )
    checker = _TagChecker()
    checker.feed(value)
    checker.close()
    # Сущности парсер не пропускает через handle_data, поэтому «голые» & ищем в сыром тексте
    if re.search(r"<[^>]*$", value):  # «<b» в конце: парсер молча отбрасывает хвост
        checker.fail("незакрытый тег в конце текста")
    if any(not _ENTITY.match(value, m.start()) for m in re.finditer("&", value)):
        checker.fail("неэкранированный символ «&» (используйте &amp;)")
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
        value = value.strip()
        if spec.max_len is not None and len(value) > spec.max_len:
            raise SettingsValidationError(key, f"Не длиннее {spec.max_len} символов.")
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise SettingsValidationError(key, "Идентификатор не должен содержать пробелов.")
        return value, warnings

    if not isinstance(value, str):
        raise SettingsValidationError(key, "Нужна строка.")

    if kind is Kind.TIME:
        value = value.strip()
        if parse_hhmm(value) is None:
            raise SettingsValidationError(key, "Время должно быть в формате ЧЧ:ММ, например 17:00.")
        return value, warnings
    if kind is Kind.URL:
        value = value.strip()
        max_len = spec.max_len or URL_MAX_LEN
        if len(value) > max_len:
            raise SettingsValidationError(key, f"Ссылка не длиннее {max_len} символов.")
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise SettingsValidationError(
                key, "В ссылке не должно быть пробелов и управляющих символов."
            )
        if value and (not value.startswith("https://") or len(value) <= len("https://")):
            raise SettingsValidationError(key, "Ссылка должна начинаться с https://")
        # Кавычки и скобки разорвали бы атрибут href="…" в посте
        if any(ch in value for ch in "\"'<>\\"):
            raise SettingsValidationError(key, "В ссылке недопустимы символы \" ' < > \\")
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
        if spec.max_len is None:
            return value, warnings
        # Худший случай: длинное имя, длинное место и т. п.
        rendered = value.format(**WORST_CASE_VARS)
        if key in MESSAGE_TEXT_KEYS:
            if len(value) > spec.max_len or len(rendered) > TELEGRAM_MESSAGE_LIMIT:
                raise SettingsValidationError(
                    key,
                    f"Текст слишком длинный: шаблон до {spec.max_len} символов, а с самыми "
                    f"длинными подстановками — до {TELEGRAM_MESSAGE_LIMIT} (лимит Telegram).",
                )
        elif len(rendered) > spec.max_len:
            raise SettingsValidationError(
                key,
                f"Текст слишком длинный: до {len(rendered)} из {spec.max_len} символов "
                "с самыми длинными подстановками (лимит всплывающего окна Telegram).",
            )
        return value, warnings

    # Kind.STR
    value = value.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise SettingsValidationError(key, "Без переводов строк и управляющих символов.")
    if spec.max_len is not None and len(value) > spec.max_len:
        raise SettingsValidationError(key, f"Не длиннее {spec.max_len} символов.")
    return value, warnings


def validate_place(value: Any) -> str:
    """Проверка места встречи по схеме `default_place`; возвращает нормализованное значение."""
    normalized, _ = validate(get_spec("default_place"), value)
    return str(normalized)


def validate_url(value: Any) -> str:
    """Проверка ссылки на карту; пустая строка допустима и значит «без ссылки»."""
    normalized, _ = validate(get_spec("default_place_url"), value)
    return str(normalized)


def validate_capacity(value: Any) -> int:
    normalized, _ = validate(get_spec("capacity"), value)
    return int(normalized)


def validate_description(value: Any) -> list[str]:
    """Проверка HTML-описания; ошибки — SettingsValidationError, предупреждения — списком."""
    _, warnings = validate(get_spec("default_description"), value)
    return warnings


__all__ = [
    "NAME_MAX_LEN",
    "REASON_MAX_LEN",
    "SAMPLE_VARS",
    "SCHEMA",
    "TEXT_PREFIX",
    "WORST_CASE_VARS",
    "Kind",
    "SettingSpec",
    "SettingsValidationError",
    "check_html",
    "check_template",
    "get_spec",
    "parse_hhmm",
    "validate",
    "validate_capacity",
    "validate_description",
    "validate_place",
    "validate_url",
]
