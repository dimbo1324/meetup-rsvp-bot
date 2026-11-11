"""Результаты и данные, которые сервис встреч отдаёт интерфейсу (значения, а не тексты)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class EventListItem:
    id: int
    status: str
    starts_at: datetime
    capacity: int
    seats_taken: int


@dataclass(frozen=True)
class CreatedEvent:
    event_id: int
    # Нефатальные замечания (например, слишком длинное описание) — показать админу
    warnings: list[str]
    # Расчётный дедлайн отмены уже прошёл и приравнен к моменту создания: отменить запись
    # без штрафа нельзя, админа стоит предупредить
    deadline_clamped: bool = False


@dataclass(frozen=True)
class RescheduleResult:
    # Закрытая встреча открыта заново автоматически (новые моменты в будущем)
    registration_reopened: bool
    # Дедлайн отмены при переносе уже прошёл и приравнен к моменту переноса
    deadline_clamped: bool = False


@dataclass(frozen=True)
class EventCard:
    """Всё, что нужно карточке встречи в меню: поля, счётчики и допустимые действия."""

    id: int
    status: str
    starts_at: datetime
    ends_at: datetime
    place: str
    place_url: str
    description_html: str
    photo_file_id: str | None
    capacity: int
    seats_taken: int
    waitlist_count: int
    cancel_deadline_at: datetime
    registration_closes_at: datetime
    reminder_at: datetime
    attendance_prompt_at: datetime
    cancel_reason: str | None
    attendance_marked: bool
    editable: bool
    can_close: bool
    can_open: bool
    can_cancel: bool
    can_reschedule: bool
    can_mark_attendance: bool
