"""Отметка явки и её исправление (раздел 5.8, сценарий 4.6).

Явка отмечается только у завершённой встречи (`finished`: наступили и начало, и закрытие
записи): пока запись открыта, состав участников ещё меняется.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import Attendance, Event, EventStatus, RegistrationStatus
from meetup_bot.repositories import audit, events, registrations
from meetup_bot.repositories import users as users_repo
from meetup_bot.services import access
from meetup_bot.services.admin_support import Provided
from meetup_bot.services.bans import BanOutcome, BanService
from meetup_bot.services.errors import DomainError, ErrorCode, InvalidOperation, NotFound
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.names import display_name
from meetup_bot.services.rules import RulesSettings

logger = logging.getLogger(__name__)

# Окно отметки и исправления явки, считая от начала встречи
ATTENDANCE_WINDOW = timedelta(days=7)


def attendance_window_open(event: Event, now: datetime) -> bool:
    """Можно ли сейчас отмечать или исправлять явку: встреча завершена и окно не истекло."""
    return event.status == EventStatus.FINISHED.value and now <= event.starts_at + ATTENDANCE_WINDOW


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
class SheetInfo:
    """Всё для листа явки: данные встречи, режим и участники."""

    event_id: int
    starts_at: datetime
    ends_at: datetime
    place: str
    # Явка уже отмечена: нажатие на имя сразу правит БД, «Готово» не нужна
    correction: bool
    # До какого момента можно отмечать и исправлять
    window_ends_at: datetime
    rows: list[SheetRow]


@dataclass(frozen=True)
class MarkResult:
    no_shows: tuple[int, ...]  # id записей, отмеченных как неявка
    new_bans: tuple[int, ...]  # id пользователей, получивших автобан
    # Пользователи, для которых пересчёт бана упал: дожмёт BanRecheckTask (`retry_pending_bans`)
    failed_ban_user_ids: tuple[int, ...] = ()


class AttendanceService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        locks: EventLocks,
        ban_service: BanService,
        rules_provider: Provided[RulesSettings] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._locks = locks
        self._bans = ban_service
        # Правила читает BanService; параметр оставлен ради совместимости сборки
        self._rules = rules_provider

    async def sheet_info(self, actor_id: int, event_id: int) -> SheetInfo:
        """Данные листа явки либо код ошибки: нет прав, нет встречи, отменена, рано, поздно."""
        async with self._sessions() as session, session.begin():
            event = await self._load_finished(session, actor_id, event_id)
            now = self._clock.now()
            self._require_window(event, now)
            return SheetInfo(
                event_id=event_id,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                place=event.place,
                correction=event.attendance_marked_at is not None,
                window_ends_at=event.starts_at + ATTENDANCE_WINDOW,
                rows=await self._sheet_rows(session, event_id),
            )

    async def get_sheet(self, actor_id: int, event_id: int) -> list[SheetRow]:
        async with self._sessions() as session, session.begin():
            await self._load_finished(session, actor_id, event_id)
            return await self._sheet_rows(session, event_id)

    async def mark(
        self, actor_id: int, event_id: int, no_show_registration_ids: set[int]
    ) -> MarkResult:
        """«Готово»: записывает отметки всем активным разом, остальные считаются пришедшими."""
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_finished(session, actor_id, event_id)
            now = self._clock.now()
            if event.attendance_marked_at is not None:
                marker = (
                    await users_repo.get(session, event.attendance_marked_by)
                    if event.attendance_marked_by is not None
                    else None
                )
                raise AlreadyMarked(
                    event.attendance_marked_by,
                    display_name(marker, None) if marker is not None else None,
                )
            self._require_window(event, now)

            active = await registrations.list_active(session, event_id)
            if not no_show_registration_ids <= {reg.id for reg in active}:
                raise InvalidOperation(
                    ErrorCode.UNKNOWN_REGISTRATION, "запись не найдена среди участников"
                )
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
        new_bans, failed = await self._evaluate_bans(actor_id, event_id, no_show_users)
        return MarkResult(tuple(sorted(no_show_registration_ids)), tuple(new_bans), tuple(failed))

    async def _evaluate_bans(
        self, actor_id: int | None, event_id: int, user_ids: list[int]
    ) -> tuple[list[int], list[int]]:
        """Пересчитывает баны неявившихся; при полном успехе помечает встречу обработанной.

        Отметка явки к этому моменту уже закоммичена, поэтому сбой одного пересчёта не роняет
        остальные, а встреча остаётся «необработанной» и её дожмёт `retry_pending_bans`.
        """
        new_bans: list[int] = []
        failed: list[int] = []
        for user_id in user_ids:
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
        if not failed:
            async with self._sessions() as session, session.begin():
                event = await events.get(session, event_id)
                if event is not None:
                    event.bans_evaluated_at = self._clock.now()
        return new_bans, failed

    async def retry_pending_bans(self) -> list[int]:
        """Дожимает пересчёт банов по встречам, где он ранее упал; возвращает обработанные.

        Без проверки прав: вызывается планировщиком. Идемпотентно.
        """
        async with self._sessions() as session:
            pending = await events.unevaluated_marked_ids(session)
        done: list[int] = []
        for event_id in pending:
            async with self._sessions() as session:
                event = await events.get(session, event_id)
                if event is None:
                    continue
                marked_by = event.attendance_marked_by
                user_ids = await registrations.no_show_user_ids(session, event_id)
            _, failed = await self._evaluate_bans(marked_by, event_id, user_ids)
            if not failed:
                done.append(event_id)
        return done

    async def correct(
        self,
        actor_id: int,
        event_id: int,
        registration_id: int,
        new_attendance: Attendance,
    ) -> BanOutcome:
        """Исправляет отметку после «Готово»; результат говорит, выдан ли или снят бан.

        Принимает целевое значение, а не переключает: два админа, нажавших одновременно,
        получают одно и то же состояние, а не отменяют друг друга.
        """
        if new_attendance is Attendance.UNKNOWN:
            raise InvalidOperation(
                ErrorCode.INVALID_ATTENDANCE, "можно выбрать только пришёл или неявка"
            )
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_finished(session, actor_id, event_id)
            now = self._clock.now()
            if event.attendance_marked_at is None:
                raise InvalidOperation(ErrorCode.NOT_MARKED, "явка ещё не отмечена")
            self._require_window(event, now)
            reg = await registrations.get(session, registration_id)
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
            if previous != new_attendance.value:
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
        """Закрывает отметку для завершённых встреч старше 7 суток: все пришли, никого не баним."""
        async with self._sessions() as session:
            candidates = await events.expired_unmarked_ids(session, now - ATTENDANCE_WINDOW)
        closed: list[int] = []
        for event_id in candidates:
            async with self._locks.lock(event_id), self._sessions() as session, session.begin():
                event = await events.get(session, event_id)
                # Пока ждали блокировку, админ мог успеть отметить явку
                if (
                    event is None
                    or event.attendance_marked_at is not None
                    or event.status != EventStatus.FINISHED.value
                ):
                    continue
                for reg in await registrations.list_active(session, event_id):
                    reg.attendance = Attendance.ATTENDED.value
                event.attendance_marked_at = now
                event.attendance_marked_by = None
                # Без отметки админа никого не баним (5.8.5): пересчитывать нечего
                event.bans_evaluated_at = now
                await audit.add(
                    session, action="attendance.expire", now=now, event_id=event_id, payload={}
                )
                closed.append(event_id)
        return closed

    @staticmethod
    async def _sheet_rows(session: AsyncSession, event_id: int) -> list[SheetRow]:
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

    @staticmethod
    async def _load_finished(session: AsyncSession, actor_id: int, event_id: int) -> Event:
        event = await access.load_event_for_admin(session, actor_id, event_id, editable_only=False)
        if event.status == EventStatus.CANCELLED.value:
            # Неявки по отменённой встрече не засчитываются, отмечать нечего
            raise InvalidOperation(ErrorCode.EVENT_CANCELLED, "встреча отменена")
        if event.status != EventStatus.FINISHED.value:
            raise InvalidOperation(
                ErrorCode.ATTENDANCE_NOT_YET, "явку можно отметить после закрытия записи"
            )
        return event

    @staticmethod
    def _require_window(event: Event, now: datetime) -> None:
        if now > event.starts_at + ATTENDANCE_WINDOW:
            raise InvalidOperation(ErrorCode.ATTENDANCE_WINDOW_CLOSED, "срок отметки явки истёк")
