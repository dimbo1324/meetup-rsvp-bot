"""Подстановка значений в тексты бота: единые правила для всплывающих окон и сообщений.

Сервисы передают «сырые» параметры (`starts_at`, `deadline_at` — моменты; `place`, `name`,
`reason`, `position` — строки), а здесь они превращаются в подстановки шаблона:
`{weekday}`, `{date}`, `{time}`, `{deadline}`, `{place}`, `{name}`, `{reason}`, `{position}`.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# Значение подстановки {reason}: префикс добавляется только при непустой причине
REASON_PREFIX = "Причина: "

_SPACES = re.compile(r"[ \t]{2,}")
# Запятая перед знаком препинания или концом строки остаётся от пустой подстановки
_DANGLING_COMMA = re.compile(r"[ \t]*,[ \t]*(?=[.,!?;:)]|$)", re.MULTILINE)
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+(?=[.,!?;:])")


class _BlankMissing(defaultdict[str, str]):
    """Для format_map: отсутствующая подстановка даёт пустую строку, а не KeyError."""

    def __init__(self, values: Mapping[str, str]) -> None:
        super().__init__(str, values)


def telegram_length(text: str) -> int:
    """Длина так, как её считает Telegram: в единицах UTF-16 (эмодзи вне BMP — это 2)."""
    return len(text.encode("utf-16-le")) // 2


def clip(text: str, limit: int, ellipsis: str = "…") -> str:
    """Обрезает текст до `limit` единиц UTF-16, не разрывая символ; в конце многоточие."""
    if telegram_length(text) <= limit:
        return text
    budget = limit - telegram_length(ellipsis)
    out: list[str] = []
    used = 0
    for char in text:
        size = telegram_length(char)
        if used + size > budget:
            break
        out.append(char)
        used += size
    return "".join(out).rstrip() + ellipsis


def date_parts(moment: datetime, tz: ZoneInfo) -> tuple[str, str, str]:
    """(«суббота», «1 ноября», «17:00») в заданном часовом поясе."""
    local = moment.astimezone(tz)
    return WEEKDAYS[local.weekday()], f"{local.day} {MONTHS[local.month - 1]}", f"{local:%H:%M}"


def format_moment(moment: datetime, tz: ZoneInfo | str) -> str:
    """«суббота, 1 ноября, 17:00»."""
    weekday, date, time_ = date_parts(moment, tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz))
    return f"{weekday}, {date}, {time_}"


def text_variables(params: Mapping[str, Any], tz: ZoneInfo | str) -> dict[str, str]:
    """Сырые параметры сервиса → подстановки шаблона (без экранирования)."""
    zone = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
    values: dict[str, str] = {}
    starts_at = params.get("starts_at")
    if isinstance(starts_at, datetime):
        values["weekday"], values["date"], values["time"] = date_parts(starts_at, zone)
    deadline_at = params.get("deadline_at")
    if isinstance(deadline_at, datetime):
        _, deadline_date, deadline_time = date_parts(deadline_at, zone)
        values["deadline"] = f"{deadline_date}, {deadline_time}"
    for name in ("place", "name", "position"):
        value = params.get(name)
        if value is not None and value != "":
            values[name] = str(value)
    reason = params.get("reason")
    if reason:
        values["reason"] = f"{REASON_PREFIX}{reason}"
    return values


def tidy(text: str) -> str:
    """Убирает следы пустых подстановок: «17:00, .» → «17:00.», двойные пробелы, хвосты."""
    text = _DANGLING_COMMA.sub("", text)
    text = _SPACE_BEFORE_PUNCT.sub("", text)
    text = _SPACES.sub(" ", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def fill(template: str, values: Mapping[str, str], *, escape_html: bool) -> str:
    """Подставляет значения в уже проверенный шаблон.

    `escape_html=True` — для сообщений в parse_mode=HTML: имя или причина иначе сломали бы
    разметку. Во всплывающих окнах HTML не интерпретируется, там экранировать нельзя.
    """
    prepared = {
        name: html.escape(value, quote=False) if escape_html else value
        for name, value in values.items()
    }
    return tidy(template.format_map(_BlankMissing(prepared)))
