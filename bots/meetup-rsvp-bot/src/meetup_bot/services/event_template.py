"""Шаблон новой встречи, снятый с последней неотменённой (мастер создания подставляет его)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class EventTemplate:
    place: str
    place_url: str
    description_html: str
    photo_file_id: str | None
    capacity: int
    # Локальное время начала (часовой пояс из настроек), а не UTC
    start_time: time
