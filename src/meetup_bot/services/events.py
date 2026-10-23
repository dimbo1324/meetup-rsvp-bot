"""Создание встреч: моменты времени вычисляются один раз и сохраняются в самой встрече."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import Event, EventStatus
from meetup_bot.repositories import audit
from meetup_bot.services.timing import (
    ScheduleSettings,
    compute_times,
    next_event_start,
    validate_times,
)

DEFAULT_CAPACITY = 10


class EventService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        schedule: ScheduleSettings | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._schedule = schedule or ScheduleSettings()

    async def create_event(
        self,
        *,
        starts_at: datetime | None = None,
        capacity: int = DEFAULT_CAPACITY,
        description_html: str = "",
        place: str = "",
        place_url: str = "",
        photo_file_id: str | None = None,
        recurrence_key: str | None = None,
        created_by: int | None = None,
    ) -> int:
        """Создаёт открытую встречу и возвращает её id.

        Без `starts_at` берётся ближайшая будущая дата по настройкам расписания.
        """
        now = self._clock.now()
        times = compute_times(starts_at or next_event_start(now, self._schedule), self._schedule)
        validate_times(times, now)
        async with self._sessions() as session, session.begin():
            event = Event(
                status=EventStatus.OPEN.value,
                starts_at=times.starts_at,
                capacity=capacity,
                description_html=description_html,
                photo_file_id=photo_file_id,
                place=place,
                place_url=place_url,
                cancel_deadline_at=times.cancel_deadline_at,
                registration_closes_at=times.registration_closes_at,
                reminder_at=times.reminder_at,
                attendance_prompt_at=times.attendance_prompt_at,
                recurrence_key=recurrence_key,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            session.add(event)
            await session.flush()
            await audit.add(
                session,
                action="event.create",
                now=now,
                actor_id=created_by,
                event_id=event.id,
                payload={"starts_at": times.starts_at.isoformat(), "capacity": capacity},
            )
            return event.id
