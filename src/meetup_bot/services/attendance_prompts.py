"""Запрос явки админам и напоминание о ней в служебный чат (раздел 5.8).

Отметка о выполнении ставится в транзакции, которая коммитится до отправки: повторный тик
ничего не шлёт второй раз (раздел 11). Если окно отметки явки уже истекло, отметка всё равно
ставится, чтобы протухшая встреча не возвращалась в выборку вечно.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.repositories import admins, schedule
from meetup_bot.services.admin_support import Provided, resolve
from meetup_bot.services.attendance import ATTENDANCE_WINDOW
from meetup_bot.services.ports import Notifier
from meetup_bot.services.safe import safe_send_service
from meetup_bot.services.timing import ScheduleSettings
from meetup_bot.texts.render import format_moment
from meetup_bot.texts.service import ATTENDANCE_NOT_MARKED_TEXT

logger = logging.getLogger(__name__)

# Через сколько после запроса явки напоминаем, что её никто не отметил
ATTENDANCE_REMINDER_AFTER = timedelta(hours=14)


class AttendancePromptService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        notifier: Notifier,
        schedule_settings: Provided[ScheduleSettings] | None = None,
    ) -> None:
        self._sessions = sessions
        self._notifier = notifier
        self._schedule: Provided[ScheduleSettings] = schedule_settings or ScheduleSettings()

    async def send_prompts(self, now: datetime) -> int:
        """Шлёт каждому админу запрос явки по завершённым встречам; возвращает число встреч."""
        event_ids: list[int] = []
        async with self._sessions() as session, session.begin():
            for event in await schedule.due_attendance_prompts(session, now):
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

    async def send_reminders(self, now: datetime) -> int:
        """Напоминает в служебный чат, что явку не отметили; возвращает число встреч."""
        texts: list[str] = []
        timezone = resolve(self._schedule).timezone
        async with self._sessions() as session, session.begin():
            due = await schedule.due_attendance_reminders(
                session, now, after=ATTENDANCE_REMINDER_AFTER
            )
            for event in due:
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
