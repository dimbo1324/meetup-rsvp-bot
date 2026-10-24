"""Сборка поста встречи (раздел 5.10): чистые функции «данные → текст, клавиатура, хеш».

Рендер гарантирует лимиты Telegram: подпись к фото до 1024 символов, текст до 4096.
Лимиты считаются по видимому тексту (после разбора HTML), как это делает Telegram.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from meetup_bot.db.models import EventStatus
from meetup_bot.texts.render import MONTHS, WEEKDAYS, clip, telegram_length

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


_TAG_RE = re.compile(r"<[^>]*>")
_TOKEN_RE = re.compile(r"(<[^>]*>)")
_OPEN_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>")
_CLOSE_TAG_RE = re.compile(r"</([a-zA-Z][a-zA-Z0-9-]*)\s*>")


@dataclass(frozen=True)
class Participant:
    name: str
    guests: int = 0


@dataclass(frozen=True)
class PostData:
    event_id: int
    status: str
    starts_at: datetime
    timezone: str
    capacity: int
    seats_taken: int
    participants: Sequence[Participant]
    description_html: str = ""
    place: str = ""
    place_url: str = ""
    cancel_reason: str | None = None
    has_photo: bool = False
    photo_file_id: str | None = None
    show_names: bool = True
    plus_one_enabled: bool = False


@dataclass(frozen=True)
class ButtonSpec:
    text: str
    callback_data: str


@dataclass(frozen=True)
class RenderedPost:
    text: str
    keyboard: tuple[tuple[ButtonSpec, ...], ...]
    content_hash: str
    has_photo: bool


def signup_callback(event_id: int, action: str) -> str:
    """`r:<event_id>:<action>`, где action: `j` — записаться, `j1` — записаться +1, `c` — отмена."""
    return f"r:{event_id}:{action}"


def visible_length(markup: str) -> int:
    """Длина текста так, как её считает Telegram: без тегов, с раскрытыми сущностями, в UTF-16."""
    return telegram_length(html.unescape(_TAG_RE.sub("", markup)))


def truncate_html(markup: str, limit: int) -> str:
    """Обрезает HTML до `limit` видимых символов, закрывая открытые теги; в конце «…»."""
    if visible_length(markup) <= limit:
        return markup
    budget = max(limit - 1, 0)
    out: list[str] = []
    open_tags: list[str] = []
    for token in _TOKEN_RE.split(markup):
        if not token:
            continue
        if token.startswith("<"):
            if close := _CLOSE_TAG_RE.fullmatch(token):
                if close.group(1) in open_tags:
                    open_tags.remove(close.group(1))
            elif opened := _OPEN_TAG_RE.fullmatch(token):
                open_tags.append(opened.group(1))
            out.append(token)
            continue
        text = html.unescape(token)
        size = telegram_length(text)
        if size > budget:
            out.append(html.escape(clip(text, budget, ellipsis=""), quote=False))
            budget = 0
            break
        out.append(token)
        budget -= size
    out.append("…")
    out.extend(f"</{tag}>" for tag in reversed(open_tags))
    return "".join(out)


def _when_line(data: PostData) -> str:
    local = data.starts_at.astimezone(ZoneInfo(data.timezone))
    weekday = WEEKDAYS[local.weekday()]
    return f"📅 {weekday}, {local.day} {MONTHS[local.month - 1]}, {local:%H:%M}"


def _place_line(data: PostData) -> str:
    if not data.place and not data.place_url:
        return ""
    label = html.escape(data.place or "Место встречи", quote=False)
    if data.place_url:
        return f'📍 <a href="{html.escape(data.place_url, quote=True)}">{label}</a>'
    return f"📍 {label}"


def _status_line(data: PostData) -> str:
    if data.status == EventStatus.CLOSED.value:
        return "Запись закрыта"
    if data.status == EventStatus.FINISHED.value:
        return "Встреча прошла"
    if data.status == EventStatus.CANCELLED.value:
        reason = f": {html.escape(data.cancel_reason, quote=False)}" if data.cancel_reason else ""
        return f"Встреча отменена{reason}"
    if data.seats_taken >= data.capacity:
        return "Мест нет"
    return ""


def _participant_line(index: int, participant: Participant) -> str:
    suffix = " (+1)" if participant.guests else ""
    return f"{index}. {html.escape(participant.name, quote=False)}{suffix}"


def _compose(data: PostData, description: str, shown: int) -> str:
    blocks: list[str] = []
    if description:
        blocks.append(description)
    info = [_when_line(data)]
    if place := _place_line(data):
        info.append(place)
    blocks.append("\n".join(info))

    counter = [f"Записались: {data.seats_taken} из {data.capacity}"]
    if data.show_names and data.participants:
        counter.extend(
            _participant_line(i, p) for i, p in enumerate(data.participants[:shown], start=1)
        )
        hidden = len(data.participants) - shown
        if hidden > 0:
            counter.append(f"… и ещё {hidden}")
    blocks.append("\n".join(counter))

    if status := _status_line(data):
        blocks.append(status)
    return "\n\n".join(blocks)


def _fit(data: PostData, limit: int) -> str:
    """Сначала сокращается список имён до «… и ещё N», затем обрезается описание."""
    description = data.description_html
    for shown in range(len(data.participants), -1, -1):
        text = _compose(data, description, shown)
        if visible_length(text) <= limit:
            return text
    skeleton = visible_length(_compose(data, "", 0))
    budget = limit - skeleton - 2  # 2 символа — разделитель «\n\n» перед описанием
    description = truncate_html(description, budget) if budget > 1 else ""
    return _compose(data, description, 0)


def _keyboard(data: PostData) -> tuple[tuple[ButtonSpec, ...], ...]:
    if data.status != EventStatus.OPEN.value:
        return ()
    rows: list[tuple[ButtonSpec, ...]] = [
        (
            ButtonSpec("Записаться", signup_callback(data.event_id, "j")),
            ButtonSpec("Отменить запись", signup_callback(data.event_id, "c")),
        )
    ]
    if data.plus_one_enabled:
        rows.append((ButtonSpec("Записаться +1", signup_callback(data.event_id, "j1")),))
    return tuple(rows)


def render_post(data: PostData) -> RenderedPost:
    limit = CAPTION_LIMIT if data.has_photo else TEXT_LIMIT
    text = _fit(data, limit)
    keyboard = _keyboard(data)
    digest = hashlib.sha256(
        json.dumps(
            [text, [[(b.text, b.callback_data) for b in row] for row in keyboard]],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return RenderedPost(text=text, keyboard=keyboard, content_hash=digest, has_photo=data.has_photo)
