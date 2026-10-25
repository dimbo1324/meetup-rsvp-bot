"""Задачи планировщика (раздел 11).

Гарантия доставки «не больше одного раза»: отметка о выполнении ставится в транзакции,
которая коммитится ДО отправки сообщений. Упавший посреди рассылки бот не пришлёт никому
напоминание дважды. Задача возвращает число обработанных объектов (для лога тика).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.db.models import (
    UNFINISHED_EVENT_STATUSES,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
    User,
)
from meetup_bot.repositories import admins
from meetup_bot.services.admin_support import (
    Provided,
    RenderText,
    default_render_text,
    resolve,
    resolve_any,
    send_dms,
)
from meetup_bot.services.attendance import ATTENDANCE_WINDOW
from meetup_bot.services.lifecycle import LifecycleService, RulesProvider
from meetup_bot.services.ports import Notifier
from meetup_bot.services.post_reminders import PostReminderService
from meetup_bot.services.safe import safe_send_service
from meetup_bot.services.timing import ScheduleSettings
from meetup_bot.texts.render import format_moment
from meetup_bot.texts.service import ATTENDANCE_NOT_MARKED_TEXT

logger = logging.getLogger(__name__)

ATTENDANCE_REMINDER_AFTER = timedelta(hours=14)


class ScheduledTask(Protocol):
    name: str

    async def run(self, now: datetime) -> int:
        """Выполняет невыполненное на момент `now`; возвращает число обработанных объектов."""
        ...


class AttendanceExpirer(Protocol):
    async def expire_unmarked(self, now: datetime) -> list[int]:
        """Закрывает окно отметки явки; возвращает id затронутых встреч."""
        ...


class BanRechecker(Protocol):
    async def retry_pending_bans(self) -> list[int]:
        """Дожимает упавший пересчёт банов; возвращает id обработанных встреч."""
        ...


class CloseRegistrationTask:
    name = "close_registration"

    def __init__(self, lifecycle: LifecycleService) -> None:
        self._lifecycle = lifecycle

    async def run(self, now: datetime) -> int:
        return len(await self._lifecycle.close_due(now))


class FinishEventsTask:
    name = "finish_events"

    def __init__(self, lifecycle: LifecycleService) -> None:
        self._lifecycle = lifecycle

    async def run(self, now: datetime) -> int:
        return len(await self._lifecycle.finish_due(now))


class AutoCreateTask:
    name = "auto_create"

    def __init__(self, lifecycle: LifecycleService, rules_provider: RulesProvider) -> None:
        self._lifecycle = lifecycle
        self._rules_provider = rules_provider

    async def run(self, now: datetime) -> int:
        rules = await resolve_any(self._rules_provider)
        result = await self._lifecycle.create_weekly_event(rules, now)
        logger.debug("автосоздание: %s", result.status.value)
        return 1 if result.event_id is not None else 0


class ReminderTask:
    name = "reminder"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        notifier: Notifier,
        rules_provider: RulesProvider,
        render_text: RenderText | None = None,
    ) -> None:
        self._sessions = sessions
        self._notifier = notifier
        self._rules_provider = rules_provider
        self._render: RenderText = render_text or default_render_text

    async def run(self, now: datetime) -> int:
        if not (await resolve_any(self._rules_provider)).reminder_enabled:
            return 0
        batches: list[tuple[Event, list[int]]] = []
        async with self._sessions() as session, session.begin():
            due = await session.scalars(
                select(Event)
                .where(
                    Event.status.in_(UNFINISHED_EVENT_STATUSES),
                    Event.reminder_at <= now,
                    Event.reminder_sent_at.is_(None),
                )
                .order_by(Event.id)
            )
            for event in due.all():
                # Отметка ставится всегда, в том числе у «протухших»: иначе они вечно в выборке
                event.reminder_sent_at = now
                if now > event.starts_at:
                    logger.warning("напоминание встречи %s пропущено: она уже началась", event.id)
                    continue
                batches.append((event, await self._recipients(session, event.id)))
        for event, user_ids in batches:
            await self._send(event, user_ids)
        return len(batches)

    @staticmethod
    async def _recipients(session: AsyncSession, event_id: int) -> list[int]:
        rows = await session.scalars(
            select(User.id)
            .join(Registration, Registration.user_id == User.id)
            .where(
                Registration.event_id == event_id,
                Registration.status == RegistrationStatus.ACTIVE.value,
                User.dm_available.is_(True),
            )
            .order_by(Registration.created_at, Registration.id)
        )
        return list(rows.all())

    async def _send(self, event: Event, user_ids: list[int]) -> None:
        text = self._render(
            "reminder",
            {
                "starts_at": event.starts_at,
                "deadline_at": event.cancel_deadline_at,
                "place": event.place,
            },
        )
        await send_dms(self._notifier, user_ids, text)


class PostReminderTask:
    name = "post_reminder"

    def __init__(self, service: PostReminderService) -> None:
        self._service = service

    async def run(self, now: datetime) -> int:
        return len(await self._service.send_due(now))


class AttendancePromptTask:
    name = "attendance_prompt"

    def __init__(self, sessions: async_sessionmaker[AsyncSession], notifier: Notifier) -> None:
        self._sessions = sessions
        self._notifier = notifier

    async def run(self, now: datetime) -> int:
        event_ids: list[int] = []
        async with self._sessions() as session, session.begin():
            due = await session.scalars(
                select(Event)
                .where(
                    Event.status == EventStatus.FINISHED.value,
                    Event.attendance_marked_at.is_(None),
                    Event.attendance_prompt_at <= now,
                    Event.attendance_prompt_sent_at.is_(None),
                )
                .order_by(Event.id)
            )
            for event in due.all():
                event.attendance_prompt_sent_at = now
                if now > event.starts_at + ATTENDANCE_WINDOW:
                    logger.warning("запрос явки встречи %s пропущен: окно истекло", event.id)
                    continue
                event_ids.append(event.id)
            admin_ids = await admins.list_admin_ids(session) if event_ids else []
        for event_id in event_ids:
            for admin_id in admin_ids:
                try:
                    await self._notifier.send_attendance_prompt(admin_id, event_id)
                except Exception:
                    logger.exception(
                        "запрос явки встречи %s админу %s не отправлен", event_id, admin_id
                    )
        return len(event_ids)


class AttendanceReminderTask:
    name = "attendance_reminder"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        notifier: Notifier,
        schedule: Provided[ScheduleSettings] | None = None,
    ) -> None:
        self._sessions = sessions
        self._notifier = notifier
        self._schedule_source: Provided[ScheduleSettings] = schedule or ScheduleSettings()

    async def run(self, now: datetime) -> int:
        texts: list[str] = []
        timezone = resolve(self._schedule_source).timezone
        async with self._sessions() as session, session.begin():
            due = await session.scalars(
                select(Event)
                .where(
                    Event.status == EventStatus.FINISHED.value,
                    Event.attendance_prompt_sent_at.is_not(None),
                    Event.attendance_prompt_sent_at <= now - ATTENDANCE_REMINDER_AFTER,
                    Event.attendance_marked_at.is_(None),
                    Event.attendance_reminder_sent_at.is_(None),
                )
                .order_by(Event.id)
            )
            for event in due.all():
                event.attendance_reminder_sent_at = now
                if now > event.starts_at + ATTENDANCE_WINDOW:
                    logger.warning(
                        "напоминание про явку встречи %s пропущено: окно истекло", event.id
                    )
                    continue
                when = format_moment(event.starts_at, timezone)
                texts.append(ATTENDANCE_NOT_MARKED_TEXT.format(when=when))
        for text in texts:
            await safe_send_service(self._notifier, text)
        return len(texts)


class AttendanceExpireTask:
    name = "attendance_expire"

    def __init__(self, attendance_service: AttendanceExpirer) -> None:
        self._service = attendance_service

    async def run(self, now: datetime) -> int:
        return len(await self._service.expire_unmarked(now))


class BanRecheckTask:
    """Повторяет пересчёт банов, упавший после отметки явки (сбой БД, перезапуск бота)."""

    name = "ban_recheck"

    def __init__(self, attendance_service: BanRechecker) -> None:
        self._service = attendance_service

    async def run(self, now: datetime) -> int:
        return len(await self._service.retry_pending_bans())
