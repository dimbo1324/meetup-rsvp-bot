"""Задачи планировщика (раздел 11).

Гарантия доставки «не больше одного раза»: отметка о выполнении ставится в транзакции,
которая коммитится ДО отправки сообщений. Упавший посреди рассылки бот не пришлёт никому
напоминание дважды. Задача возвращает число обработанных объектов (для лога тика).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from meetup_bot.services.admin_support import Provided, resolve
from meetup_bot.services.attendance_prompts import AttendancePromptService
from meetup_bot.services.lifecycle import LifecycleService
from meetup_bot.services.post_reminders import PostReminderService
from meetup_bot.services.reminders import ReminderService
from meetup_bot.services.rules import RulesSettings

logger = logging.getLogger(__name__)


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

    def __init__(
        self, lifecycle: LifecycleService, rules_provider: Provided[RulesSettings]
    ) -> None:
        self._lifecycle = lifecycle
        self._rules_provider = rules_provider

    async def run(self, now: datetime) -> int:
        rules = resolve(self._rules_provider)
        result = await self._lifecycle.create_weekly_event(rules, now)
        logger.debug("автосоздание: %s", result.status.value)
        return 1 if result.event_id is not None else 0


class ReminderTask:
    name = "reminder"

    def __init__(self, service: ReminderService) -> None:
        self._service = service

    async def run(self, now: datetime) -> int:
        return await self._service.send_due(now)


class PostReminderTask:
    name = "post_reminder"

    def __init__(self, service: PostReminderService) -> None:
        self._service = service

    async def run(self, now: datetime) -> int:
        return len(await self._service.send_due(now))


class AttendancePromptTask:
    name = "attendance_prompt"

    def __init__(self, service: AttendancePromptService) -> None:
        self._service = service

    async def run(self, now: datetime) -> int:
        return await self._service.send_prompts(now)


class AttendanceReminderTask:
    name = "attendance_reminder"

    def __init__(self, service: AttendancePromptService) -> None:
        self._service = service

    async def run(self, now: datetime) -> int:
        return await self._service.send_reminders(now)


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
