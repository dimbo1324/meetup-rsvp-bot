"""CSV-выгрузка списка участников (Приложение В спецификации)."""

from __future__ import annotations

import csv
import io
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import RegistrationStatus
from meetup_bot.repositories import audit, events, events_admin
from meetup_bot.services import access
from meetup_bot.services.errors import NotFound
from meetup_bot.services.names import display_name

HEADER = [
    "event_date",
    "position",
    "name",
    "username",
    "user_id",
    "profile_link",
    "status",
    "guests",
    "registered_at",
    "attendance",
]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def _safe(value: str) -> str:
    """Защита от CSV-инъекции: Excel исполняет ячейки, начинающиеся с формульных символов."""
    # Excel пропускает ведущие пробелы и всё равно исполняет формулу, поэтому смотрим и на lstrip
    dangerous = value.startswith(_FORMULA_PREFIXES) or value.lstrip().startswith(_FORMULA_PREFIXES)
    return "'" + value if dangerous else value


class ExportService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock

    async def build_csv(self, actor_id: int, event_id: int, timezone: str) -> bytes:
        """Все записи встречи: сначала активные и очередь по порядку, затем отменённые/удалённые."""
        tz = ZoneInfo(timezone)
        now = self._clock.now()
        async with self._sessions() as session:
            await access.require_admin(session, actor_id)
            event = await events.get(session, event_id)
            if event is None:
                raise NotFound(f"встреча {event_id} не найдена")
            rows = await events_admin.list_with_users(session, event_id)
            await audit.add(
                session, action="event.export", now=now, actor_id=actor_id, event_id=event_id
            )
            await session.commit()

        event_date = event.starts_at.astimezone(tz).strftime("%Y-%m-%d")
        order = {
            RegistrationStatus.ACTIVE.value: 0,
            RegistrationStatus.WAITLIST.value: 1,
            RegistrationStatus.CANCELLED.value: 2,
            RegistrationStatus.REMOVED.value: 3,
        }
        rows.sort(key=lambda row: (order[row[0].status], row[0].created_at, row[0].id))

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(HEADER)
        counters = {RegistrationStatus.ACTIVE.value: 0, RegistrationStatus.WAITLIST.value: 0}
        for registration, user in rows:
            position = ""
            if registration.status in counters:
                counters[registration.status] += 1
                position = str(counters[registration.status])
            writer.writerow(
                [
                    event_date,
                    position,
                    _safe(display_name(user, registration.manual_name)),
                    _safe(user.username or "") if user else "",
                    registration.user_id if registration.user_id is not None else "",
                    f"tg://user?id={registration.user_id}" if registration.user_id else "",
                    registration.status,
                    registration.guests,
                    registration.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                    registration.attendance,
                ]
            )
        return buffer.getvalue().encode("utf-8-sig")
