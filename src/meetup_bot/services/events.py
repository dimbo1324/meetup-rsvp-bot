"""Встречи: создание и админские действия над ними (разделы 5.2 и 5.7 спецификации).

Моменты времени вычисляются один раз и сохраняются в самой встрече. Все операции, меняющие
места, идут под блокировкой встречи и в одной транзакции; уведомления и пересборка постов
выполняются только после коммита.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import Event, EventStatus
from meetup_bot.repositories import admins, audit, events, events_admin
from meetup_bot.services.admin_support import (
    RenderText,
    default_render_text,
    format_local,
    send_dms,
)
from meetup_bot.services.errors import InvalidOperation, NotFound
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.ports import Notifier, NullNotifier, NullPostRefresher, PostRefresher
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.timing import (
    EventTimes,
    InvalidEventTimes,
    ScheduleSettings,
    compute_times,
    next_event_start,
    validate_times,
)
from meetup_bot.services.waitlist import promote_waitlist

DEFAULT_CAPACITY = 10
MAX_CAPACITY = 200
RECENT_DAYS = 14


class _Unset(enum.Enum):
    TOKEN = enum.auto()


# Отличает «не менять поле» от «обнулить поле» (например, убрать фото)
UNSET = _Unset.TOKEN


@dataclass(frozen=True)
class EventListItem:
    id: int
    status: str
    starts_at: datetime
    capacity: int
    seats_taken: int


class EventService:
    """Настройки расписания и правил можно передать значением или провайдером (Callable).

    Провайдер вызывается при каждой операции, поэтому изменения настроек из админки
    подхватываются без перезапуска; обычное значение удобно в тестах.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        schedule: ScheduleSettings | Callable[[], ScheduleSettings] | None = None,
        *,
        locks: EventLocks | None = None,
        notifier: Notifier | None = None,
        post_refresher: PostRefresher | None = None,
        rules: RulesSettings | Callable[[], RulesSettings] | None = None,
        render_text: RenderText | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._schedule = schedule
        self._locks = locks or EventLocks()
        self._notifier: Notifier = notifier or NullNotifier()
        self._refresher: PostRefresher = post_refresher or NullPostRefresher()
        self._rules = rules
        self._render: RenderText = render_text or default_render_text

    def _schedule_settings(self) -> ScheduleSettings:
        if self._schedule is None:
            return ScheduleSettings()
        return self._schedule() if callable(self._schedule) else self._schedule

    def _rules_settings(self) -> RulesSettings:
        if self._rules is None:
            return RulesSettings()
        return self._rules() if callable(self._rules) else self._rules

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
        schedule = self._schedule_settings()
        times = compute_times(starts_at or next_event_start(now, schedule), schedule)
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

    async def list_events(self, actor_id: int, include_recent: bool = False) -> list[EventListItem]:
        """Открытые и закрытые встречи; с `include_recent` — ещё и всё за последние 14 дней."""
        now = self._clock.now()
        recent_since = now - timedelta(days=RECENT_DAYS) if include_recent else None
        async with self._sessions() as session:
            await admins.require_admin(session, actor_id)
            found = await events_admin.list_events(session, recent_since=recent_since)
            return [
                EventListItem(
                    id=event.id,
                    status=event.status,
                    starts_at=event.starts_at,
                    capacity=event.capacity,
                    seats_taken=await events.seats_taken(session, event.id),
                )
                for event in found
            ]

    async def update_details(
        self,
        actor_id: int,
        event_id: int,
        *,
        description_html: str | _Unset = UNSET,
        place: str | _Unset = UNSET,
        place_url: str | _Unset = UNSET,
        photo_file_id: str | _Unset | None = UNSET,
    ) -> None:
        """Меняет только переданные поля; `photo_file_id=None` убирает фото."""
        now = self._clock.now()
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            changed: list[str] = []
            if not isinstance(description_html, _Unset):
                event.description_html = description_html
                changed.append("description_html")
            if not isinstance(place, _Unset):
                event.place = place
                changed.append("place")
            if not isinstance(place_url, _Unset):
                event.place_url = place_url
                changed.append("place_url")
            if not isinstance(photo_file_id, _Unset):
                event.photo_file_id = photo_file_id
                changed.append("photo_file_id")
            event.updated_at = now
            await audit.add(
                session,
                action="event.update",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                payload={"fields": changed},
            )
        self._refresher.schedule(event_id)

    async def reschedule(self, actor_id: int, event_id: int, new_starts_at: datetime) -> None:
        """Переносит встречу: все моменты пересчитываются по 5.2, записанным уходит уведомление."""
        now = self._clock.now()
        schedule = self._schedule_settings()
        times = compute_times(new_starts_at, schedule)
        try:
            validate_times(times, now)
        except InvalidEventTimes as exc:
            raise InvalidOperation("invalid_times", str(exc)) from exc
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            old_starts_at = event.starts_at
            self._apply_times(event, times)
            event.updated_at = now
            await audit.add(
                session,
                action="event.reschedule",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                payload={
                    "old_starts_at": old_starts_at.isoformat(),
                    "new_starts_at": times.starts_at.isoformat(),
                },
            )
            recipients = await events_admin.dm_recipients(session, event_id, events_admin.LIVE)
        self._refresher.schedule(event_id)
        text = self._render(
            "event_moved", {"starts_at": format_local(times.starts_at, schedule.timezone)}
        )
        await send_dms(self._notifier, recipients, text)

    async def set_capacity(self, actor_id: int, event_id: int, new_capacity: int) -> None:
        if not 1 <= new_capacity <= MAX_CAPACITY:
            raise InvalidOperation("capacity_out_of_range")
        now = self._clock.now()
        late_hours = self._rules_settings().late_promotion_hours
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            taken = await events.seats_taken(session, event_id)
            if new_capacity < taken:
                raise InvalidOperation("capacity_below_taken")
            old_capacity = event.capacity
            event.capacity = new_capacity
            event.updated_at = now
            promotions = []
            if new_capacity > old_capacity:
                await session.flush()
                promotions = await promote_waitlist(
                    session, event, now, late_promotion_hours=late_hours
                )
            await audit.add(
                session,
                action="event.capacity",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                payload={
                    "old": old_capacity,
                    "new": new_capacity,
                    "promoted": [p.registration_id for p in promotions],
                },
            )
            promoted_ids = [p.user_id for p in promotions if p.user_id is not None]
            recipients = await events_admin.filter_dm_available(session, promoted_ids)
            starts_at = event.starts_at
        self._refresher.schedule(event_id)
        text = self._render(
            "promoted",
            {"starts_at": format_local(starts_at, self._schedule_settings().timezone)},
        )
        await send_dms(self._notifier, recipients, text)

    async def set_registration_open(self, actor_id: int, event_id: int, open: bool) -> None:
        now = self._clock.now()
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            await admins.require_admin(session, actor_id)
            event = await events.get(session, event_id)
            if event is None:
                raise NotFound(f"встреча {event_id} не найдена")
            current = event.status
            if current not in (EventStatus.OPEN.value, EventStatus.CLOSED.value):
                raise InvalidOperation("bad_status")
            if open == (current == EventStatus.OPEN.value):
                return
            if open:
                if now >= event.starts_at:
                    raise InvalidOperation("already_started")
                event.status = EventStatus.OPEN.value
                event.closed_at = None
                # Иначе запись осталась бы закрытой по времени, а планировщик закрыл бы её снова
                if event.registration_closes_at <= now:
                    event.registration_closes_at = event.starts_at
                action = "event.open"
            else:
                event.status = EventStatus.CLOSED.value
                event.closed_at = now
                action = "event.close"
            event.updated_at = now
            await audit.add(session, action=action, now=now, actor_id=actor_id, event_id=event_id)
        self._refresher.schedule(event_id)

    async def cancel_event(self, actor_id: int, event_id: int, reason: str | None = None) -> None:
        """Отменяет встречу. Неявки по ней не считаются: бан-логика исключает cancelled."""
        now = self._clock.now()
        reason = (reason or "").strip() or None
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            event.status = EventStatus.CANCELLED.value
            event.cancelled_at = now
            event.cancel_reason = reason
            event.updated_at = now
            await audit.add(
                session,
                action="event.cancel",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                payload={"reason": reason},
            )
            recipients = await events_admin.dm_recipients(
                session, event_id, events_admin.ACTIVE_ONLY
            )
            starts_at = event.starts_at
        self._refresher.schedule(event_id)
        text = self._render(
            "event_cancelled",
            {
                "starts_at": format_local(starts_at, self._schedule_settings().timezone),
                "reason": reason or "",
                "reason_line": f" Причина: {reason}" if reason else "",
            },
        )
        await send_dms(self._notifier, recipients, text)

    async def edit_moments(
        self,
        actor_id: int,
        event_id: int,
        *,
        cancel_deadline_at: datetime | None = None,
        registration_closes_at: datetime | None = None,
        reminder_at: datetime | None = None,
        attendance_prompt_at: datetime | None = None,
    ) -> None:
        """Ручная правка моментов; инварианты 5.2 проверяются по итоговому набору."""
        now = self._clock.now()
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            new = EventTimes(
                starts_at=event.starts_at,
                cancel_deadline_at=cancel_deadline_at or event.cancel_deadline_at,
                registration_closes_at=registration_closes_at or event.registration_closes_at,
                reminder_at=reminder_at or event.reminder_at,
                attendance_prompt_at=attendance_prompt_at or event.attendance_prompt_at,
            )
            try:
                # Порядок считается от создания встречи, а не от «сейчас»: иначе нельзя было бы
                # поправить напоминание у встречи, дедлайн отмены которой уже прошёл
                validate_times(new, event.created_at)
            except InvalidEventTimes as exc:
                raise InvalidOperation("invalid_times", str(exc)) from exc
            changed: dict[str, Any] = {}
            if new.reminder_at != event.reminder_at:
                event.reminder_sent_at = None
            if new.attendance_prompt_at != event.attendance_prompt_at:
                event.attendance_prompt_sent_at = None
                event.attendance_reminder_sent_at = None
            for name in (
                "cancel_deadline_at",
                "registration_closes_at",
                "reminder_at",
                "attendance_prompt_at",
            ):
                value: datetime = getattr(new, name)
                if value != getattr(event, name):
                    changed[name] = value.isoformat()
                    setattr(event, name, value)
            event.updated_at = now
            await audit.add(
                session,
                action="event.moments",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                payload=changed,
            )
        self._refresher.schedule(event_id)

    @staticmethod
    def _apply_times(event: Event, times: EventTimes) -> None:
        event.starts_at = times.starts_at
        event.cancel_deadline_at = times.cancel_deadline_at
        event.registration_closes_at = times.registration_closes_at
        event.reminder_at = times.reminder_at
        event.attendance_prompt_at = times.attendance_prompt_at
        # Моменты сдвинулись, поэтому напоминания и опрос явки должны сработать заново
        event.reminder_sent_at = None
        event.attendance_prompt_sent_at = None
        event.attendance_reminder_sent_at = None

    @staticmethod
    async def _load_editable(session: AsyncSession, actor_id: int, event_id: int) -> Event:
        await admins.require_admin(session, actor_id)
        event = await events.get(session, event_id)
        if event is None:
            raise NotFound(f"встреча {event_id} не найдена")
        if event.status not in (EventStatus.OPEN.value, EventStatus.CLOSED.value):
            raise InvalidOperation("bad_status")
        return event
