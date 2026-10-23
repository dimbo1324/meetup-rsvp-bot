"""Жизненный цикл встречи по времени: закрытие записи, завершение, автосоздание (5.1, 5.9).

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
from sqlalchemy.orm import InstrumentedAttribute

from meetup_bot.clock import Clock
from meetup_bot.db.models import Event, EventStatus
from meetup_bot.repositories import audit
from meetup_bot.services.admin_support import (
    Provided,
    chunk_lines,
    format_local,
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

logger = logging.getLogger(__name__)

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
    # Целевая дата уже прошла (бот долго лежал): создавать задним числом бессмысленно
    SKIPPED_PAST = "skipped_past"
    # Настройки дают недопустимые моменты или поля встречи: создать нечего, ждём правки настроек
    INVALID = "invalid"


@dataclass(frozen=True)
class AutoCreateResult:
    status: AutoCreateStatus
    recurrence_key: str | None = None
    event_id: int | None = None


def last_auto_create_moment(now: datetime, rules: RulesSettings, timezone: str) -> datetime:
    """Последний момент `auto_create_weekday` + `auto_create_time`, не позже `now` (локальный)."""
    tz = ZoneInfo(timezone)
    local_now = now.astimezone(tz)
    days_back = (local_now.weekday() - rules.auto_create_weekday) % 7
    candidate = datetime.combine(
        local_now.date() - timedelta(days=days_back), rules.auto_create_time, tzinfo=tz
    )
    if candidate > local_now:
        candidate -= timedelta(days=7)
    return candidate


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
        for event_id in await self._due_ids(EventStatus.OPEN, Event.registration_closes_at, now):
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
                starts_at = event.starts_at
            closed.append(event_id)
            safe_schedule(self._refresher, event_id)
            # Если встреча уже началась (долгий простой), итог в чат уже не нужен
            if now < starts_at:
                await self._send_closed_summary(event_id)
        return closed

    async def finish_due(self, now: datetime) -> list[int]:
        """Переводит в `finished` встречи, у которых наступил `starts_at`."""
        finished: list[int] = []
        for status in (EventStatus.OPEN, EventStatus.CLOSED):
            for event_id in await self._due_ids(status, Event.starts_at, now):
                async with (
                    self._locks.lock(event_id),
                    self._sessions() as session,
                    session.begin(),
                ):
                    event = await session.get(Event, event_id)
                    if (
                        event is None
                        or event.status not in (EventStatus.OPEN.value, EventStatus.CLOSED.value)
                        or event.starts_at > now
                    ):
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
        # Цель считаем от последнего момента автосоздания, а не от «сейчас»: иначе сразу после
        # субботней встречи создалась бы встреча на неделю вперёд раньше положенного срока
        schedule = self._schedule
        last_moment = last_auto_create_moment(now, rules, schedule.timezone)
        starts_at = next_event_start(last_moment, schedule)
        if starts_at <= now:
            return AutoCreateResult(AutoCreateStatus.SKIPPED_PAST)
        key = f"weekly:{starts_at.astimezone(ZoneInfo(schedule.timezone)).date().isoformat()}"
        if await self._key_exists(key):
            return AutoCreateResult(AutoCreateStatus.ALREADY_EXISTS, key)

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

        when = format_local(starts_at, schedule.timezone)
        await safe_send_service(self._notifier, f"Создана встреча на {when}.")
        return AutoCreateResult(AutoCreateStatus.CREATED, key, event_id)

    async def _due_ids(
        self, status: EventStatus, column: InstrumentedAttribute[datetime], now: datetime
    ) -> list[int]:
        # Выборка без блокировок: решающая проверка повторяется под блокировкой встречи
        async with self._sessions() as session:
            stmt = select(Event.id).where(
                Event.status == status.value,
                column <= now,
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
        when = format_local(data.starts_at, timezone)
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
