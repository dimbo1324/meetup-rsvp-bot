"""Напоминание участникам о встрече (раздел 5.8, задача планировщика `reminder`).

Отметка `reminder_sent_at` коммитится ДО отправки: упавший посреди рассылки бот не пришлёт
никому напоминание дважды (раздел 11).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.repositories import events_admin, schedule
from meetup_bot.services.admin_support import (
    Provided,
    RenderText,
    default_render_text,
    resolve,
    send_dms,
)
from meetup_bot.services.ports import Notifier
from meetup_bot.services.rules import RulesSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Batch:
    text_params: dict[str, object]
    user_ids: list[int]


class ReminderService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        notifier: Notifier,
        rules: Provided[RulesSettings],
        render_text: RenderText | None = None,
    ) -> None:
        self._sessions = sessions
        self._notifier = notifier
        self._rules = rules
        self._render: RenderText = render_text or default_render_text

    async def send_due(self, now: datetime) -> int:
        """Напоминает по встречам, чей срок наступил; возвращает число обработанных встреч."""
        if not resolve(self._rules).reminder_enabled:
            return 0
        batches: list[_Batch] = []
        async with self._sessions() as session, session.begin():
            for event in await schedule.due_reminders(session, now):
                # Отметка ставится всегда, в том числе у «протухших»: иначе они вечно в выборке
                event.reminder_sent_at = now
                if now > event.starts_at:
                    logger.warning("напоминание встречи %s пропущено: она уже началась", event.id)
                    continue
                recipients = await events_admin.dm_recipients(
                    session, event.id, events_admin.ACTIVE_ONLY
                )
                params: dict[str, object] = {
                    "starts_at": event.starts_at,
                    "ends_at": event.ends_at,
                    "deadline_at": event.cancel_deadline_at,
                    "place": event.place,
                }
                batches.append(_Batch(params, recipients))
        for batch in batches:
            await send_dms(
                self._notifier, batch.user_ids, self._render("reminder", batch.text_params)
            )
        return len(batches)
