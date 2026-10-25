"""Жизненный цикл встречи по времени: закрытие записи, завершение, автосоздание (5.1, 5.9).

Запись закрывается не в момент начала, а позже (опоздавшие ещё могут записаться), поэтому
встреча завершается только после закрытия записи.

Каждый метод идемпотентен: выбирается только невыполненное, статус перепроверяется под
блокировкой встречи, поэтому повторный или параллельный вызов ничего не дублирует.
Уведомления и пересборка постов идут после коммита: сбой Telegram не откатывает статус.
"""

from __future__ import annotations

import enum
import html
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import Event, EventStatus
from meetup_bot.repositories import audit
from meetup_bot.services.admin_support import (
    Provided,
    chunk_lines,
    resolve,
    resolve_any,
)
from meetup_bot.services.errors import InvalidOperation
from meetup_bot.services.events import DEFAULT_CAPACITY, EventService
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.ports import Notifier, PostRefresher
from meetup_bot.services.posts import load_post_data
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.safe import safe_schedule, safe_send_service
from meetup_bot.services.timing import (
    InvalidEventTimes,
    ScheduleSettings,
    next_event_start,
)
from meetup_bot.texts.render import format_moment
from meetup_bot.texts.service import AUTO_CREATED_TEXT

logger = logging.getLogger(__name__)

# Сколько после закрытия записи итог ещё актуален, даже если запрос явки совпал с закрытием
CLOSED_SUMMARY_GRACE = timedelta(minutes=30)

# Подсказка «как разослать пост»: бот сам в чаты не публикует, это делает админ

# Провайдер может быть и синхронным, и асинхронным
RulesProvider = Callable[[], RulesSettings | Awaitable[RulesSettings]]


@dataclass(frozen=True)
class EventDefaults:
    """Параметры встречи, создаваемой автоматически (берутся из настроек)."""

    capacity: int = DEFAULT_CAPACITY
    description_html: str = ""
    place: str = ""
    place_url: str = ""
    photo_file_id: str | None = None


DefaultsProvider = Callable[[], EventDefaults | Awaitable[EventDefaults]]


class AutoCreateStatus(enum.StrEnum):
    CREATED = "created"
    ALREADY_EXISTS = "already_exists"
    DISABLED = "disabled"
    # Момент создания ближайшей встречи ещё не наступил
    NOT_DUE = "not_due"
    # Настройки дают недопустимые моменты или поля встречи: создать нечего, ждём правки настроек
    INVALID = "invalid"


@dataclass(frozen=True)
class AutoCreateResult:
    status: AutoCreateStatus
    recurrence_key: str | None = None
    event_id: int | None = None


_UNFINISHED = (EventStatus.OPEN.value, EventStatus.CLOSED.value)


def _finish_due(event: Event, now: datetime) -> bool:
    return (
        event.status in _UNFINISHED
        and event.starts_at <= now
        and event.registration_closes_at <= now
    )


def closed_summary_relevant(event: Event, now: datetime) -> bool:
    """Нужен ли итог записи в служебный чат при закрытии по времени.

    После долгого простоя бота мог наступить и запрос явки: тогда список уже придёт в нём,
    а итог «запись закрыта» только зашумит чат. Запрос явки может совпадать с закрытием
    (он не бывает раньше него), поэтому своевременному закрытию дан запас.
    """
    deadline = max(event.attendance_prompt_at, event.registration_closes_at + CLOSED_SUMMARY_GRACE)
    return now < deadline


class LifecycleService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        locks: EventLocks,
        notifier: Notifier,
        post_refresher: PostRefresher,
        *,
        events: EventService | None = None,
        schedule: Provided[ScheduleSettings] | None = None,
        defaults_provider: DefaultsProvider | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._locks = locks
        self._notifier = notifier
        self._refresher = post_refresher
        self._schedule_source: Provided[ScheduleSettings] = schedule or ScheduleSettings()
        # Тот же реестр блокировок, что и у остальных сервисов: иначе автосоздание и админские
        # правки одной встречи не исключали бы друг друга
        self._events = events or EventService(
            sessions,
            clock,
            self._schedule_source,
            locks=locks,
            notifier=notifier,
            post_refresher=post_refresher,
        )
        self._defaults_provider = defaults_provider
        # Ключ, по которому уже предупредили о невозможности автосоздания: не засоряем лог
        self._warned_invalid_key: str | None = None

    @property
    def _schedule(self) -> ScheduleSettings:
        return resolve(self._schedule_source)

    async def close_due(self, now: datetime) -> list[int]:
        """Закрывает запись у встреч, у которых наступил `registration_closes_at`."""
        closed: list[int] = []
        for event_id in await self._close_due_ids(now):
            async with self._locks.lock(event_id), self._sessions() as session, session.begin():
                event = await session.get(Event, event_id)
                # Статус могли сменить (админ закрыл или отменил) между выборкой и блокировкой
                if (
                    event is None
                    or event.status != EventStatus.OPEN.value
                    or event.registration_closes_at > now
                ):
                    continue
                event.status = EventStatus.CLOSED.value
                event.closed_at = now
                event.updated_at = now
                await audit.add(
                    session,
                    action="event.close",
                    now=now,
                    event_id=event_id,
                    payload={"by": "scheduler"},
                )
                send_summary = closed_summary_relevant(event, now)
            closed.append(event_id)
            safe_schedule(self._refresher, event_id)
            if send_summary:
                await self._send_closed_summary(event_id)
        return closed

    async def finish_due(self, now: datetime) -> list[int]:
        """Переводит в `finished` встречи, у которых наступили и начало, и закрытие записи."""
        finished: list[int] = []
        for event_id in await self._finish_due_ids(now):
            async with self._locks.lock(event_id), self._sessions() as session, session.begin():
                event = await session.get(Event, event_id)
                if event is None or not _finish_due(event, now):
                    continue
                event.status = EventStatus.FINISHED.value
                event.updated_at = now
                await audit.add(
                    session,
                    action="event.finish",
                    now=now,
                    event_id=event_id,
                    payload={"by": "scheduler"},
                )
            finished.append(event_id)
            safe_schedule(self._refresher, event_id)
        return finished

    async def create_weekly_event(self, rules: RulesSettings, now: datetime) -> AutoCreateResult:
        """Автосоздание встречи (5.9); повторный вызов дубль не создаёт."""
        if not rules.auto_create_enabled:
            return AutoCreateResult(AutoCreateStatus.DISABLED)
        # Цели — две ближайшие будущие встречи: пропущенную за простой задним числом не создаём;
        # вторая нужна, когда момент создания наступает до начала текущей встречи (0 дней после)
        schedule = self._schedule
        tz = ZoneInfo(schedule.timezone)
        nearest = next_event_start(now, schedule)
        found_existing: str | None = None
        for starts_at in (nearest, next_event_start(nearest, schedule)):
            target_date = starts_at.astimezone(tz).date()
            offset = rules.auto_create_days_after - 7
            due_date = target_date + timedelta(days=offset)
            # Локальное время: «10:00» не должно плыть вместе со сдвигом UTC
            if now < datetime.combine(due_date, rules.auto_create_time, tzinfo=tz):
                continue
            key = f"weekly:{target_date.isoformat()}"
            if await self._key_exists(key):
                found_existing = found_existing or key
                continue
            return await self._create_for(starts_at, key, schedule)
        if found_existing is not None:
            return AutoCreateResult(AutoCreateStatus.ALREADY_EXISTS, found_existing)
        return AutoCreateResult(AutoCreateStatus.NOT_DUE)

    async def _create_for(
        self, starts_at: datetime, key: str, schedule: ScheduleSettings
    ) -> AutoCreateResult:
        defaults = EventDefaults()
        if self._defaults_provider is not None:
            defaults = await resolve_any(self._defaults_provider)
        try:
            event_id = await self._events.create_event(
                starts_at=starts_at,
                capacity=defaults.capacity,
                description_html=defaults.description_html,
                place=defaults.place,
                place_url=defaults.place_url,
                photo_file_id=defaults.photo_file_id,
                recurrence_key=key,
            )
        except IntegrityError:
            # Гонка с другим создателем: уникальный индекс recurrence_key сработал, встреча есть
            logger.info("автосоздание %s: встреча уже существует (IntegrityError)", key)
            return AutoCreateResult(AutoCreateStatus.ALREADY_EXISTS, key)
        except (InvalidEventTimes, InvalidOperation) as exc:
            # Плохие настройки не починятся сами; предупреждаем один раз на ключ, а не каждый тик
            if self._warned_invalid_key != key:
                self._warned_invalid_key = key
                logger.warning("автосоздание %s невозможно: %s", key, exc)
            return AutoCreateResult(AutoCreateStatus.INVALID, key)

        when = format_moment(starts_at, schedule.timezone)
        await safe_send_service(self._notifier, AUTO_CREATED_TEXT.format(when=when))
        return AutoCreateResult(AutoCreateStatus.CREATED, key, event_id)

    async def _close_due_ids(self, now: datetime) -> list[int]:
        # Выборка без блокировок: решающая проверка повторяется под блокировкой встречи
        async with self._sessions() as session:
            stmt = select(Event.id).where(
                Event.status == EventStatus.OPEN.value,
                Event.registration_closes_at <= now,
            )
            return list((await session.scalars(stmt.order_by(Event.id))).all())

    async def _finish_due_ids(self, now: datetime) -> list[int]:
        async with self._sessions() as session:
            stmt = select(Event.id).where(
                Event.status.in_(_UNFINISHED),
                Event.starts_at <= now,
                Event.registration_closes_at <= now,
            )
            return list((await session.scalars(stmt.order_by(Event.id))).all())

    async def _key_exists(self, key: str) -> bool:
        async with self._sessions() as session:
            found = await session.scalar(select(Event.id).where(Event.recurrence_key == key))
            return found is not None

    async def _send_closed_summary(self, event_id: int) -> None:
        timezone = self._schedule.timezone
        data = await load_post_data(self._sessions, event_id, timezone=timezone)
        if data is None:
            return
        when = format_moment(data.starts_at, timezone)
        lines = [
            f"Запись на встречу ({when}) закрыта.",
            f"Заполнено {data.seats_taken} из {data.capacity}.",
        ]
        for index, participant in enumerate(data.participants, start=1):
            suffix = " (+1)" if participant.guests else ""
            lines.append(f"{index}. {html.escape(participant.name, quote=False)}{suffix}")
        # Длинный список не помещается в одно сообщение Telegram
        for chunk in chunk_lines(lines):
            await safe_send_service(self._notifier, chunk)
