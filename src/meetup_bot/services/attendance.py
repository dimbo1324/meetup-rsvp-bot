"""Отметка явки и её исправление (раздел 5.8, сценарий 4.6)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import (
    Attendance,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
    User,
)
from meetup_bot.repositories import admins, audit, events, registrations
from meetup_bot.services.bans import BanOutcome, BanService
from meetup_bot.services.errors import DomainError, InvalidOperation, NotFound
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.participants import display_name
from meetup_bot.services.rules import RulesSettings

logger = logging.getLogger(__name__)

# Окно отметки и исправления явки, считая от начала встречи
ATTENDANCE_WINDOW = timedelta(days=7)


class AlreadyMarked(DomainError):
    """Явку уже отметил другой админ; побеждает первый нажавший «Готово»."""

    def __init__(self, marked_by_id: int | None, marked_by_name: str | None) -> None:
        super().__init__(f"явка уже отмечена: {marked_by_name or marked_by_id}")
        self.marked_by_id = marked_by_id
        self.marked_by_name = marked_by_name


@dataclass(frozen=True)
class SheetRow:
    registration_id: int
    user_id: int | None
    name: str
    guests: int
    attendance: str


@dataclass(frozen=True)
class MarkResult:
    marked_by: int
    no_shows: tuple[int, ...]  # id записей, отмеченных как неявка
    new_bans: tuple[int, ...]  # id пользователей, получивших автобан
    # Пользователи, для которых пересчёт бана упал: дожать через `reevaluate_bans`
    failed_ban_user_ids: tuple[int, ...] = ()


class AttendanceService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        locks: EventLocks,
        ban_service: BanService,
        rules_provider: Callable[[], RulesSettings] = RulesSettings,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._locks = locks
        self._bans = ban_service
        self._rules = rules_provider

    async def get_sheet(self, actor_id: int, event_id: int) -> list[SheetRow]:
        async with self._sessions() as session, session.begin():
            await admins.require_admin(session, actor_id)
            event = await self._require_event(session, event_id)
            self._require_started(event, self._clock.now())
            rows = await registrations.list_active_with_users(session, event_id)
            return [
                SheetRow(
                    registration_id=reg.id,
                    user_id=reg.user_id,
                    name=display_name(user, reg.manual_name),
                    guests=reg.guests,
                    attendance=reg.attendance,
                )
                for reg, user in rows
            ]

    async def mark(
        self, actor_id: int, event_id: int, no_show_registration_ids: set[int]
    ) -> MarkResult:
        """«Готово»: записывает отметки всем активным разом, остальные считаются пришедшими."""
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            await admins.require_admin(session, actor_id)
            now = self._clock.now()
            event = await self._require_event(session, event_id)
            self._require_started(event, now)
            if event.attendance_marked_at is not None:
                marker = (
                    await session.get(User, event.attendance_marked_by)
                    if event.attendance_marked_by is not None
                    else None
                )
                raise AlreadyMarked(
                    event.attendance_marked_by,
                    display_name(marker, None) if marker is not None else None,
                )
            self._require_window(event, now)

            active = await self._active_registrations(session, event_id)
            if not no_show_registration_ids <= {reg.id for reg in active}:
                raise InvalidOperation("unknown_registration", "запись не найдена среди участников")
            for reg in active:
                reg.attendance = (
                    Attendance.NO_SHOW.value
                    if reg.id in no_show_registration_ids
                    else Attendance.ATTENDED.value
                )
            event.attendance_marked_at = now
            event.attendance_marked_by = actor_id
            await audit.add(
                session,
                action="attendance.mark",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                payload={"no_show_registration_ids": sorted(no_show_registration_ids)},
            )
            await session.flush()
            # Включая отменённых админом «как неявка»: их неявка ещё не была учтена в бане
            no_show_users = await registrations.no_show_user_ids(session, event_id)

        # Пересчёт банов вне блокировки этой встречи: бан берёт блокировки будущих встреч
        new_bans: list[int] = []
        failed: list[int] = []
        for user_id in no_show_users:
            # Отметка уже закоммичена: сбой одного пересчёта не должен ронять остальные
            try:
                outcome = await self._bans.recalc_for_attendance(actor_id, user_id, event_id)
            except Exception:
                logger.exception(
                    "не удалось пересчитать бан (user_id=%s, event_id=%s)", user_id, event_id
                )
                failed.append(user_id)
                continue
            if outcome.banned:
                new_bans.append(user_id)
        return MarkResult(
            actor_id,
            tuple(sorted(no_show_registration_ids)),
            tuple(new_bans),
            tuple(failed),
        )

    async def reevaluate_bans(self, event_id: int) -> list[int]:
        """Досчитывает баны по уже отмеченной встрече; возвращает выданные автобаны.

        Без проверки прав: для системы и планировщика (дожать сбои из `mark`). Идемпотентно.
        """
        async with self._sessions() as session, session.begin():
            event = await events.get(session, event_id)
            if event is None or event.attendance_marked_at is None:
                return []
            marked_by = event.attendance_marked_by
            user_ids = await registrations.no_show_user_ids(session, event_id)
        new_bans: list[int] = []
        for user_id in user_ids:
            try:
                outcome = await self._bans.recalc_for_attendance(marked_by, user_id, event_id)
            except Exception:
                logger.exception(
                    "не удалось пересчитать бан (user_id=%s, event_id=%s)", user_id, event_id
                )
                continue
            if outcome.banned:
                new_bans.append(user_id)
        return new_bans

    async def correct(
        self,
        actor_id: int,
        event_id: int,
        registration_id: int,
        new_attendance: Attendance,
    ) -> BanOutcome:
        """Исправляет отметку после «Готово»; результат говорит, выдан ли или снят бан."""
        if new_attendance is Attendance.UNKNOWN:
            raise InvalidOperation("invalid_attendance", "можно выбрать только пришёл или неявка")
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            await admins.require_admin(session, actor_id)
            now = self._clock.now()
            event = await self._require_event(session, event_id)
            if event.attendance_marked_at is None:
                raise InvalidOperation("not_marked", "явка ещё не отмечена")
            self._require_window(event, now)
            reg = await session.get(Registration, registration_id)
            # Отменённую админом «как неявка» запись можно исправить: так снимается ошибочный штраф
            correctable = reg is not None and (
                reg.status == RegistrationStatus.ACTIVE.value
                or (
                    reg.status == RegistrationStatus.CANCELLED.value
                    and reg.attendance == Attendance.NO_SHOW.value
                )
            )
            if reg is None or reg.event_id != event_id or not correctable:
                raise NotFound(f"запись {registration_id} не найдена")
            previous = reg.attendance
            reg.attendance = new_attendance.value
            await audit.add(
                session,
                action="attendance.correct",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                target_user_id=reg.user_id,
                payload={
                    "registration_id": registration_id,
                    "from": previous,
                    "to": new_attendance.value,
                },
            )
            user_id = reg.user_id
        if user_id is None:
            return BanOutcome(None)
        return await self._bans.recalc_for_attendance(actor_id, user_id, event_id)

    async def expire_unmarked(self, now: datetime) -> list[int]:
        """Закрывает отметку для встреч старше 7 суток: все пришли, никого не баним."""
        async with self._sessions() as session, session.begin():
            candidates = list(
                (
                    await session.scalars(
                        select(Event.id).where(
                            Event.attendance_marked_at.is_(None),
                            Event.status != EventStatus.CANCELLED.value,
                            Event.starts_at < now - ATTENDANCE_WINDOW,
                        )
                    )
                ).all()
            )
        closed: list[int] = []
        for event_id in candidates:
            async with self._locks.lock(event_id), self._sessions() as session, session.begin():
                event = await events.get(session, event_id)
                # Пока ждали блокировку, админ мог успеть отметить явку
                if event is None or event.attendance_marked_at is not None:
                    continue
                for reg in await self._active_registrations(session, event_id):
                    reg.attendance = Attendance.ATTENDED.value
                event.attendance_marked_at = now
                event.attendance_marked_by = None
                await audit.add(
                    session, action="attendance.expire", now=now, event_id=event_id, payload={}
                )
                closed.append(event_id)
        return closed

    @staticmethod
    async def _active_registrations(session: AsyncSession, event_id: int) -> list[Registration]:
        rows = await session.scalars(
            select(Registration).where(
                Registration.event_id == event_id,
                Registration.status == RegistrationStatus.ACTIVE.value,
            )
        )
        return list(rows.all())

    @staticmethod
    async def _require_event(session: AsyncSession, event_id: int) -> Event:
        event = await events.get(session, event_id)
        if event is None:
            raise NotFound(f"встреча {event_id} не найдена")
        if event.status == EventStatus.CANCELLED.value:
            # Неявки по отменённой встрече не засчитываются, отмечать нечего
            raise InvalidOperation("event_cancelled", "встреча отменена")
        return event

    @staticmethod
    def _require_started(event: Event, now: datetime) -> None:
        if event.status != EventStatus.FINISHED.value and event.starts_at > now:
            raise InvalidOperation("event_not_started", "встреча ещё не началась")

    @staticmethod
    def _require_window(event: Event, now: datetime) -> None:
        if now > event.starts_at + ATTENDANCE_WINDOW:
            raise InvalidOperation("attendance_window_closed", "срок отметки явки истёк")
