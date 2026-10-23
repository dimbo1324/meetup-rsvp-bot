"""Общие мелочи админских сервисов: шаблоны текстов, провайдеры настроек, рассылка после коммита."""

from __future__ import annotations

import html
import inspect
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from meetup_bot.renderer.post import MONTHS, WEEKDAYS
from meetup_bot.services.ports import Notifier
from meetup_bot.services.safe import safe_send_dm

# Совместимо с будущим сервисом текстов: (ключ, параметры) -> готовая строка
RenderText = Callable[[str, dict[str, Any]], str]

# Значение или синхронный провайдер: провайдер вызывается при каждой операции,
# поэтому смена настроек в админке подхватывается без перезапуска
type Provided[T] = T | Callable[[], T]
# Провайдер, который может быть и синхронным, и асинхронным
type AnyProvider[T] = Callable[[], T | Awaitable[T]]

# Лимит Telegram — 4096 символов; с запасом на разметку
MESSAGE_CHUNK = 3500

# Параметры, куда попадает пользовательский ввод; ЛС и служебный чат идут в parse_mode=HTML
_USER_PARAMS = ("reason", "place", "name")

_DEFAULT_TEMPLATES = {
    "event_moved": "Встреча перенесена: новое время — {starts_at}.",
    "event_cancelled": "Встреча {starts_at} отменена.{reason_line}",
    "promoted": "Освободилось место — вы записаны на встречу {starts_at}.",
}


def default_render_text(key: str, params: dict[str, Any]) -> str:
    safe = escape_params(params)
    safe["reason_line"] = f" Причина: {safe['reason']}" if safe.get("reason") else ""
    return _DEFAULT_TEMPLATES[key].format(**safe)


def escape_params(params: dict[str, Any]) -> dict[str, Any]:
    """Экранирует пользовательские значения перед подстановкой в HTML-шаблон."""
    return {
        name: html.escape(value, quote=False) if name in _USER_PARAMS else value
        for name, value in params.items()
    }


def format_local(moment: datetime, timezone: str) -> str:
    """«суббота, 1 ноября, 17:00» в локальном часовом поясе."""
    local = moment.astimezone(ZoneInfo(timezone))
    return f"{WEEKDAYS[local.weekday()]}, {local.day} {MONTHS[local.month - 1]}, {local:%H:%M}"


def resolve[T](value: T | Callable[[], T]) -> T:
    """Значение либо результат синхронного провайдера (датаклассы настроек не вызываемы)."""
    return value() if callable(value) else value


async def resolve_any[T](provider: Callable[[], T | Awaitable[T]]) -> T:
    result = provider()
    if inspect.isawaitable(result):
        return await result
    return result


def chunk_lines(lines: Iterable[str], limit: int = MESSAGE_CHUNK) -> list[str]:
    """Склеивает строки в сообщения не длиннее `limit`; строки не рвутся посередине."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def send_dms(notifier: Notifier, user_ids: list[int], text: str) -> None:
    """Рассылка после коммита: сбой отправки не должен ломать уже сохранённую операцию."""
    for user_id in user_ids:
        await safe_send_dm(notifier, user_id, text)
