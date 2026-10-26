"""Напоминание админам «пора разослать пост» (M6.2): бот сам в чаты не публикует.

Встреча, у которой к моменту напоминания нет ни одного живого поста, получает одно
сообщение в служебный чат. Отметка `post_reminder_sent_at` коммитится до отправки
(«не больше одного раза», раздел 11). Если сообщение не доставлено, отметка снимается и
напоминание повторяется на одном из следующих тиков, но не чаще `POST_REMINDER_RETRY`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.db.models import Event, EventStatus
from meetup_bot.repositories import event_posts
from meetup_bot.services.admin_support import Provided, resolve
from meetup_bot.services.defaults import POST_REMINDER_RETRY
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.ports import Notifier
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.safe import safe_send_service
from meetup_bot.services.timing import ScheduleSettings
from meetup_bot.texts.render import format_moment
from meetup_bot.texts.service import POST_REMINDER_TEXT

logger = logging.getLogger(__name__)

POST_REMINDER_DISABLED = 0


def post_reminder_moment(starts_at: datetime, rules: RulesSettings, tz: ZoneInfo) -> datetime:
    """Момент напоминания в UTC: локальная дата начала минус N дней, в заданное время."""
    local_date = starts_at.astimezone(tz).date() - timedelta(days=rules.post_reminder_days_before)
    return datetime.combine(local_date, rules.post_reminder_time, tzinfo=tz).astimezone(UTC)


class PostReminderService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        notifier: Notifier,
        rules_provider: Provided[RulesSettings],
        schedule: Provided[ScheduleSettings],
        locks: EventLocks | None = None,
    ) -> None:
        self._sessions = sessions
        self._notifier = notifier
        self._rules_provider = rules_provider
        self._schedule = schedule
        self._locks = locks or EventLocks()
        # Когда последняя попытка не доставилась; только в памяти: после перезапуска повтор
        # допустим сразу. Записи старше интервала удаляются в начале каждого прогона.
        self._failed_at: dict[int, datetime] = {}

    async def send_due(self, now: datetime) -> list[int]:
        """Напоминает о встречах без постов, чей момент наступил; возвращает их id."""
        rules = resolve(self._rules_provider)
        if rules.post_reminder_days_before == POST_REMINDER_DISABLED:
            return []
        tz = ZoneInfo(resolve(self._schedule).timezone)
        async with self._sessions() as session:
            candidates = await event_posts.list_open_without_live_posts(session, starts_after=now)
            due_ids = [
                event.id
                for event in candidates
                if post_reminder_moment(event.starts_at, rules, tz) <= now
            ]
        self._failed_at = {
            event_id: at
            for event_id, at in self._failed_at.items()
            if now - at < POST_REMINDER_RETRY
        }
        reminded: list[int] = []
        for event_id in due_ids:
            if event_id in self._failed_at:
                continue
            starts_at = await self._mark(event_id, now)
            if starts_at is None:
                continue
            reminded.append(event_id)
            text = POST_REMINDER_TEXT.format(when=format_moment(starts_at, tz))
            if not await safe_send_service(self._notifier, text):
                self._failed_at[event_id] = now
                await self._unmark(event_id, now)
        return reminded

    async def _unmark(self, event_id: int, marked_at: datetime) -> None:
        """Снимает отметку недоставленного напоминания, чтобы повторить его позже."""
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await session.get(Event, event_id)
            # Отметку мог уже сбросить и поставить заново кто-то другой (перенос встречи)
            if event is not None and event.post_reminder_sent_at == marked_at:
                event.post_reminder_sent_at = None
                logger.warning("напоминание о посте встречи %s не доставлено, повторим", event_id)

    async def _mark(self, event_id: int, now: datetime) -> datetime | None:
        """Ставит отметку и возвращает начало встречи; None — встреча уже не подходит."""
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await session.get(Event, event_id)
            # Между выборкой и блокировкой встречу могли отменить, а админ — разослать пост
            if (
                event is None
                or event.status != EventStatus.OPEN.value
                or event.post_reminder_sent_at is not None
                or event.starts_at <= now
                or await event_posts.has_live(session, event_id)
            ):
                return None
            event.post_reminder_sent_at = now
            logger.info("напоминание о рассылке поста встречи %s", event_id)
            return event.starts_at
