"""Встречи: создание и админские действия над ними (разделы 5.2 и 5.7 спецификации).

Моменты времени вычисляются один раз и сохраняются в самой встрече. Все операции, меняющие
места, идут под блокировкой встречи и в одной транзакции; уведомления и пересборка постов
выполняются только после коммита.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import (
    LIVE_REGISTRATION_STATUSES,
    UNFINISHED_EVENT_STATUSES,
    Event,
    EventStatus,
)
from meetup_bot.repositories import audit, events, events_admin, registrations
from meetup_bot.services import access
from meetup_bot.services.admin_support import (
    Provided,
    RenderText,
    default_render_text,
    notify_promotions,
    send_dms,
)
from meetup_bot.services.attendance import attendance_window_open
from meetup_bot.services.bans import BanService
from meetup_bot.services.defaults import DEFAULT_CAPACITY
from meetup_bot.services.errors import ErrorCode, InvalidOperation
from meetup_bot.services.event_results import (
    CreatedEvent,
    EventCard,
    EventListItem,
    RescheduleResult,
)
from meetup_bot.services.event_template import EventTemplate
from meetup_bot.services.event_validation import (
    max_registration_close_delay,
    validate_capacity,
    validate_description,
    validate_photo,
    validate_place,
    validate_url,
)
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.ports import Notifier, NullNotifier, NullPostRefresher, PostRefresher
from meetup_bot.services.provided import resolve_or_default
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.safe import safe_schedule
from meetup_bot.services.timing import (
    EventTimes,
    InvalidEventTimes,
    ScheduleSettings,
    clamp_deadline,
    compute_times,
    next_event_start,
    validate_times,
)
from meetup_bot.services.waitlist import Promotion, promote_waitlist

logger = logging.getLogger(__name__)

RECENT_DAYS = 14

__all__ = ["UNSET", "EventService"]


class _Unset(enum.Enum):
    TOKEN = enum.auto()


# Отличает «не менять поле» от «обнулить поле» (например, убрать фото)
UNSET = _Unset.TOKEN


class EventService:
    """Настройки расписания и правил можно передать значением или провайдером (Callable).

    Провайдер вызывается при каждой операции, поэтому изменения настроек из админки
    подхватываются без перезапуска; обычное значение удобно в тестах.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        schedule: Provided[ScheduleSettings] | None = None,
        *,
        locks: EventLocks | None = None,
        notifier: Notifier | None = None,
        post_refresher: PostRefresher | None = None,
        rules: Provided[RulesSettings] | None = None,
        render_text: RenderText | None = None,
        ban_service: BanService | None = None,
    ) -> None:
        if locks is None:
            # Отдельный реестр не синхронизирован с блокировками других сервисов, поэтому
            # в боевой сборке locks обязателен; необязательность оставлена ради тестов
            logger.warning("EventService создан без общего реестра блокировок")
            locks = EventLocks()
        self._sessions = sessions
        self._clock = clock
        self._schedule = schedule
        self._locks = locks
        self._notifier: Notifier = notifier or NullNotifier()
        self._refresher: PostRefresher = post_refresher or NullPostRefresher()
        self._rules = rules
        self._render: RenderText = render_text or default_render_text
        self._bans = ban_service

    def _schedule_settings(self) -> ScheduleSettings:
        return resolve_or_default(self._schedule, ScheduleSettings)

    def _rules_settings(self) -> RulesSettings:
        return resolve_or_default(self._rules, RulesSettings)

    async def create_event_as_admin(
        self,
        actor_id: int,
        *,
        starts_at: datetime | None = None,
        capacity: int = DEFAULT_CAPACITY,
        description_html: str = "",
        place: str = "",
        place_url: str = "",
        photo_file_id: str | None = None,
    ) -> CreatedEvent:
        """Создаёт встречу от имени админа: проверяет права и все поля.

        Handler обязан вызывать только этот метод; `create_event` прав не проверяет.
        """
        return await self._create(
            actor_id,
            starts_at=starts_at,
            capacity=capacity,
            description_html=description_html,
            place=place,
            place_url=place_url,
            photo_file_id=photo_file_id,
            recurrence_key=None,
        )

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
        """Системное создание (автосоздание, тесты) БЕЗ проверки прав; поля валидируются.

        Из handler-ов вызывать нельзя: используйте `create_event_as_admin`.
        Без `starts_at` берётся ближайшая будущая дата по настройкам расписания.
        """
        created = await self._create(
            None,
            starts_at=starts_at,
            capacity=capacity,
            description_html=description_html,
            place=place,
            place_url=place_url,
            photo_file_id=photo_file_id,
            recurrence_key=recurrence_key,
            created_by=created_by,
        )
        return created.event_id

    async def _create(
        self,
        actor_id: int | None,
        *,
        starts_at: datetime | None,
        capacity: int,
        description_html: str,
        place: str,
        place_url: str,
        photo_file_id: str | None,
        recurrence_key: str | None,
        created_by: int | None = None,
    ) -> CreatedEvent:
        now = self._clock.now()
        schedule = self._schedule_settings()
        async with self._sessions() as session, session.begin():
            if actor_id is not None:
                # Права проверяются раньше валидации: постороннему не раскрываем детали
                await access.require_admin(session, actor_id)
                created_by = actor_id
            capacity = validate_capacity(capacity)
            warnings = validate_description(description_html)
            place = validate_place(place)
            place_url = validate_url(place_url)
            photo_file_id = validate_photo(photo_file_id)
            try:
                times = compute_times(starts_at or next_event_start(now, schedule), schedule)
                times, deadline_clamped = clamp_deadline(times, now)
                validate_times(times, now)
            except ValueError as exc:  # включая InvalidEventTimes
                raise InvalidOperation(ErrorCode.INVALID_TIMES, str(exc)) from exc
            event = Event(
                status=EventStatus.OPEN.value,
                starts_at=times.starts_at,
                ends_at=times.ends_at,
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
                payload={
                    "starts_at": times.starts_at.isoformat(),
                    "capacity": capacity,
                    "deadline_clamped": deadline_clamped,
                },
            )
            event_id = event.id
        if warnings:
            logger.warning("встреча %s: %s", event_id, "; ".join(warnings))
        return CreatedEvent(event_id, warnings, deadline_clamped)

    async def list_events(self, actor_id: int, include_recent: bool = False) -> list[EventListItem]:
        """Открытые и закрытые встречи; с `include_recent` — ещё и всё за последние 14 дней."""
        now = self._clock.now()
        recent_since = now - timedelta(days=RECENT_DAYS) if include_recent else None
        async with self._sessions() as session:
            await access.require_admin(session, actor_id)
            found = await events_admin.list_events(session, recent_since=recent_since)
            taken = await events.seats_taken_map(session, [event.id for event in found])
            return [
                EventListItem(
                    id=event.id,
                    status=event.status,
                    starts_at=event.starts_at,
                    capacity=event.capacity,
                    seats_taken=taken[event.id],
                )
                for event in found
            ]

    async def get_card(self, actor_id: int, event_id: int) -> EventCard:
        """Карточка встречи любого статуса с флагами допустимых действий (для меню)."""
        now = self._clock.now()
        async with self._sessions() as session:
            event = await access.load_event_for_admin(
                session, actor_id, event_id, editable_only=False
            )
            taken = await events.seats_taken(session, event_id)
            waitlist = await registrations.count_waitlist(session, event_id)
        editable = event.status in UNFINISHED_EVENT_STATUSES
        marked = event.attendance_marked_at is not None
        return EventCard(
            id=event.id,
            status=event.status,
            starts_at=event.starts_at,
            ends_at=event.ends_at,
            place=event.place,
            place_url=event.place_url,
            description_html=event.description_html,
            photo_file_id=event.photo_file_id,
            capacity=event.capacity,
            seats_taken=taken,
            waitlist_count=waitlist,
            cancel_deadline_at=event.cancel_deadline_at,
            registration_closes_at=event.registration_closes_at,
            reminder_at=event.reminder_at,
            attendance_prompt_at=event.attendance_prompt_at,
            cancel_reason=event.cancel_reason,
            attendance_marked=marked,
            editable=editable,
            can_close=event.status == EventStatus.OPEN.value,
            can_open=event.status == EventStatus.CLOSED.value and not marked,
            can_cancel=event.status != EventStatus.CANCELLED.value,
            can_reschedule=editable and not marked,
            can_mark_attendance=attendance_window_open(event, now),
        )

    async def capacity_needs_confirmation(
        self, actor_id: int, event_id: int, new_capacity: int
    ) -> bool:
        """Уменьшение лимита до значения не ниже занятых мест — разрушительно (12.2): нужно
        подтверждение. Увеличение и невозможное уменьшение (ниже занятых) подтверждения не просят:
        второе отклонит `set_capacity`.
        """
        new_capacity = validate_capacity(new_capacity)
        async with self._sessions() as session:
            event = await access.load_event_for_admin(
                session, actor_id, event_id, editable_only=True
            )
            taken = await events.seats_taken(session, event_id)
        return taken <= new_capacity < event.capacity

    async def last_event_template(self, actor_id: int) -> EventTemplate | None:
        """Поля самой поздней по началу неотменённой встречи; None, если таких нет."""
        tz = ZoneInfo(self._schedule_settings().timezone)
        async with self._sessions() as session:
            await access.require_admin(session, actor_id)
            event = await events_admin.latest_not_cancelled(session)
            if event is None:
                return None
            local = event.starts_at.astimezone(tz)
            return EventTemplate(
                place=event.place,
                place_url=event.place_url,
                description_html=event.description_html,
                photo_file_id=event.photo_file_id,
                capacity=event.capacity,
                start_time=time(local.hour, local.minute),
            )

    async def update_details(
        self,
        actor_id: int,
        event_id: int,
        *,
        description_html: str | _Unset = UNSET,
        place: str | _Unset = UNSET,
        place_url: str | _Unset = UNSET,
        photo_file_id: str | _Unset | None = UNSET,
    ) -> list[str]:
        """Меняет только переданные поля; `photo_file_id=None` убирает фото.

        Возвращает предупреждения (например, о длинном описании).
        """
        now = self._clock.now()
        warnings: list[str] = []
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            changed: list[str] = []
            if not isinstance(description_html, _Unset):
                warnings = validate_description(description_html)
                event.description_html = description_html
                changed.append("description_html")
            if not isinstance(place, _Unset):
                event.place = validate_place(place)
                changed.append("place")
            if not isinstance(place_url, _Unset):
                event.place_url = validate_url(place_url)
                changed.append("place_url")
            if not isinstance(photo_file_id, _Unset):
                event.photo_file_id = validate_photo(photo_file_id)
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
        safe_schedule(self._refresher, event_id)
        return warnings

    async def reschedule(
        self, actor_id: int, event_id: int, new_starts_at: datetime
    ) -> RescheduleResult:
        """Переносит встречу: все моменты пересчитываются по 5.2, записанным уходит уведомление.

        Длительность встречи сохраняется: её могли задать вручную, а не из настроек.
        Закрытая встреча открывается заново автоматически: новое начало в будущем, а закрытие
        записи не раньше начала.
        """
        now = self._clock.now()
        schedule = self._schedule_settings()
        try:
            computed = compute_times(new_starts_at, schedule)
        except ValueError as exc:
            raise InvalidOperation(ErrorCode.INVALID_TIMES, str(exc)) from exc
        if computed.starts_at <= now:
            raise InvalidOperation(ErrorCode.INVALID_TIMES, "Новое время встречи уже прошло.")
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            self._require_attendance_not_marked(event)
            times = replace(
                computed, ends_at=computed.starts_at + (event.ends_at - event.starts_at)
            )
            times, deadline_clamped = clamp_deadline(times, now)
            try:
                # Порядок считается от создания встречи, а не от «сейчас» (как в edit_moments):
                # иначе нельзя было бы перенести встречу на близкую дату
                validate_times(times, event.created_at)
            except InvalidEventTimes as exc:
                raise InvalidOperation(ErrorCode.INVALID_TIMES, str(exc)) from exc
            old_starts_at = event.starts_at
            self._apply_times(event, times)
            event.updated_at = now
            recipients = await events_admin.dm_recipients(
                session, event_id, LIVE_REGISTRATION_STATUSES
            )
            reopen = event.status == EventStatus.CLOSED.value and times.registration_closes_at > now
            promotions: list[Promotion] = []
            if reopen:
                promotions = await self._reopen(session, event, now)
            place = event.place
            await audit.add(
                session,
                action="event.reschedule",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                payload={
                    "old_starts_at": old_starts_at.isoformat(),
                    "new_starts_at": times.starts_at.isoformat(),
                    "reopened": reopen,
                    "deadline_clamped": deadline_clamped,
                },
            )
        safe_schedule(self._refresher, event_id)
        text = self._render(
            "event_moved", {"starts_at": times.starts_at, "ends_at": times.ends_at, "place": place}
        )
        # Продвинутые из очереди получают только «место освободилось» с уже новым временем
        promoted_ids = {p.user_id for p in promotions}
        await send_dms(self._notifier, [u for u in recipients if u not in promoted_ids], text)
        await notify_promotions(self._notifier, self._render, promotions)
        return RescheduleResult(registration_reopened=reopen, deadline_clamped=deadline_clamped)

    async def set_capacity(self, actor_id: int, event_id: int, new_capacity: int) -> None:
        new_capacity = validate_capacity(new_capacity)
        now = self._clock.now()
        late_hours = self._rules_settings().late_promotion_hours
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            taken = await events.seats_taken(session, event_id)
            if new_capacity < taken:
                raise InvalidOperation(ErrorCode.CAPACITY_BELOW_TAKEN)
            old_capacity = event.capacity
            event.capacity = new_capacity
            event.updated_at = now
            promotions: list[Promotion] = []
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
        safe_schedule(self._refresher, event_id)
        await notify_promotions(self._notifier, self._render, promotions)

    async def set_registration_open(self, actor_id: int, event_id: int, open: bool) -> None:
        """Открывает или закрывает запись; при открытии освободившиеся места раздаёт очередь.

        Закрыть можно в любой момент до завершения встречи, в том числе после начала. Открыть —
        только пока не наступило закрытие записи по времени: иначе планировщик закрыл бы её снова.
        """
        now = self._clock.now()
        promotions: list[Promotion] = []
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            current = event.status
            if open == (current == EventStatus.OPEN.value):
                return
            if open:
                self._require_attendance_not_marked(event)
                if now >= event.registration_closes_at:
                    raise InvalidOperation(
                        ErrorCode.REGISTRATION_TIME_PASSED,
                        "Время записи вышло: сначала перенесите закрытие записи.",
                    )
                promotions = await self._reopen(session, event, now)
                action = "event.open"
            else:
                event.status = EventStatus.CLOSED.value
                event.closed_at = now
                action = "event.close"
            event.updated_at = now
            await audit.add(session, action=action, now=now, actor_id=actor_id, event_id=event_id)
        safe_schedule(self._refresher, event_id)
        await notify_promotions(self._notifier, self._render, promotions)

    async def cancel_event(self, actor_id: int, event_id: int, reason: str | None = None) -> None:
        """Отменяет встречу. Неявки по ней не считаются: бан-логика исключает cancelled.

        Можно отменить и завершённую встречу (её не было, а явка уже отмечена): автобаны
        по её неявкам пересчитываются после коммита.
        """
        now = self._clock.now()
        reason = (reason or "").strip() or None
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await access.load_event_for_admin(
                session, actor_id, event_id, editable_only=False
            )
            if event.status == EventStatus.CANCELLED.value:
                raise InvalidOperation(ErrorCode.BAD_STATUS)
            was_finished = event.status == EventStatus.FINISHED.value
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
            no_show_users = await registrations.no_show_user_ids(session, event_id)
        safe_schedule(self._refresher, event_id)
        # Значения передаются сырыми: экранирует рендер текстов (parse_mode=HTML у ЛС)
        params = {"starts_at": starts_at, "reason": reason or ""}
        # О завершённой встрече участникам уже не пишем: отмена нужна, чтобы исправить баны
        if not was_finished:
            await send_dms(self._notifier, recipients, self._render("event_cancelled", params))
        await self._recalc_bans_after_cancel(actor_id, event_id, no_show_users)

    async def _recalc_bans_after_cancel(
        self, actor_id: int, event_id: int, user_ids: list[int]
    ) -> None:
        """Неявки отменённой встречи не считаются: автобаны по ней снимаются.

        Вне блокировки встречи (бан берёт блокировки будущих встреч). Сбой одного пересчёта
        не должен терять уже сохранённую отмену и остальные пересчёты.
        """
        if self._bans is None:
            return
        for user_id in user_ids:
            try:
                await self._bans.recalc_for_attendance(actor_id, user_id, event_id)
            except Exception:
                logger.exception(
                    "не удалось пересчитать бан после отмены (user_id=%s, event_id=%s)",
                    user_id,
                    event_id,
                )

    async def edit_moments(
        self,
        actor_id: int,
        event_id: int,
        *,
        ends_at: datetime | None = None,
        cancel_deadline_at: datetime | None = None,
        registration_closes_at: datetime | None = None,
        reminder_at: datetime | None = None,
        attendance_prompt_at: datetime | None = None,
    ) -> None:
        """Ручная правка моментов; инварианты 5.2 проверяются по итоговому набору.

        Закрытие записи может быть позже начала (опоздавшие записываются, пока есть места),
        но не дальше предела настройки. Если закрытие сдвинуто позже запроса явки, а запрос
        не передан, он поднимается до закрытия, как в `compute_times`.
        """
        now = self._clock.now()
        given = (
            ends_at,
            cancel_deadline_at,
            registration_closes_at,
            reminder_at,
            attendance_prompt_at,
        )
        if any(value is not None and value.tzinfo is None for value in given):
            raise InvalidOperation(ErrorCode.INVALID_TIMES, "Время должно быть с часовым поясом.")
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_editable(session, actor_id, event_id)
            closes = registration_closes_at or event.registration_closes_at
            if closes > event.starts_at + max_registration_close_delay():
                raise InvalidOperation(
                    ErrorCode.INVALID_TIMES, "Закрытие записи слишком далеко от начала встречи."
                )
            prompt = attendance_prompt_at or max(event.attendance_prompt_at, closes)
            new = EventTimes(
                starts_at=event.starts_at,
                ends_at=ends_at or event.ends_at,
                cancel_deadline_at=cancel_deadline_at or event.cancel_deadline_at,
                registration_closes_at=closes,
                reminder_at=reminder_at or event.reminder_at,
                attendance_prompt_at=prompt,
            )
            try:
                # Порядок считается от создания встречи, а не от «сейчас»: иначе нельзя было бы
                # поправить напоминание у встречи, дедлайн отмены которой уже прошёл
                validate_times(new, event.created_at)
            except InvalidEventTimes as exc:
                raise InvalidOperation(ErrorCode.INVALID_TIMES, str(exc)) from exc
            changed: dict[str, Any] = {}
            if new.reminder_at != event.reminder_at:
                event.reminder_sent_at = None
            if new.attendance_prompt_at != event.attendance_prompt_at:
                event.attendance_prompt_sent_at = None
                event.attendance_reminder_sent_at = None
            for name in (
                "ends_at",
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
        safe_schedule(self._refresher, event_id)

    async def _reopen(self, session: AsyncSession, event: Event, now: datetime) -> list[Promotion]:
        """Открывает запись и раздаёт освободившиеся места очереди (под блокировкой встречи)."""
        event.status = EventStatus.OPEN.value
        event.closed_at = None
        await session.flush()
        return await promote_waitlist(
            session, event, now, late_promotion_hours=self._rules_settings().late_promotion_hours
        )

    @staticmethod
    def _apply_times(event: Event, times: EventTimes) -> None:
        event.starts_at = times.starts_at
        event.ends_at = times.ends_at
        event.cancel_deadline_at = times.cancel_deadline_at
        event.registration_closes_at = times.registration_closes_at
        event.reminder_at = times.reminder_at
        event.attendance_prompt_at = times.attendance_prompt_at
        # Моменты сдвинулись, поэтому напоминания и опрос явки должны сработать заново
        event.reminder_sent_at = None
        event.post_reminder_sent_at = None
        event.attendance_prompt_sent_at = None
        event.attendance_reminder_sent_at = None

    @staticmethod
    def _require_attendance_not_marked(event: Event) -> None:
        """После отметки явки время и запись менять нельзя: неявки уже учтены в банах."""
        if event.attendance_marked_at is not None:
            raise InvalidOperation(ErrorCode.ATTENDANCE_ALREADY_MARKED)

    @staticmethod
    async def _load_editable(session: AsyncSession, actor_id: int, event_id: int) -> Event:
        return await access.load_event_for_admin(session, actor_id, event_id, editable_only=True)
