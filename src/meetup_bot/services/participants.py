"""Участники встречи под управлением админа (раздел 5.7 и сценарий 4.7 спецификации)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import (
    LIVE_REGISTRATION_STATUSES,
    UNFINISHED_EVENT_STATUSES,
    Attendance,
    Event,
    Registration,
    RegistrationSource,
    RegistrationStatus,
)
from meetup_bot.repositories import audit, events, events_admin, registrations, users
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services import access
from meetup_bot.services.admin_support import (
    RenderText,
    default_render_text,
    notify_promotions,
    resolve,
)
from meetup_bot.services.bans import BanService
from meetup_bot.services.errors import ErrorCode, InvalidOperation, NotFound
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.names import display_name
from meetup_bot.services.ports import Notifier, NullNotifier, NullPostRefresher, PostRefresher
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.safe import safe_schedule
from meetup_bot.services.waitlist import Promotion, promote_waitlist

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AddResult:
    registration_id: int
    seats_taken: int
    capacity: int
    # Лимит превышен с подтверждением админа (force)
    over_capacity: bool


@dataclass(frozen=True)
class RemoveResult:
    registration_id: int
    user_id: int | None
    promoted: tuple[Promotion, ...]
    # True: неявка записана и бан нужно пересчитать для `user_id`. Если сервис создан с
    # `ban_service`, пересчёт уже выполнен (см. `banned`); иначе его делает вызывающий
    ban_recalc_required: bool = False
    # Пересчёт привёл к выдаче автобана
    banned: bool = False


@dataclass(frozen=True)
class ParticipantRow:
    registration_id: int
    user_id: int | None
    name: str
    username: str | None
    guests: int
    status: str
    source: str
    attendance: str
    created_at: datetime
    position: int | None  # место в очереди для waitlist
    # Дойдут ли личные сообщения; у ручной записи без аккаунта писать некому
    dm_available: bool = False


@dataclass(frozen=True)
class ParticipantList:
    active: list[ParticipantRow]
    waitlist: list[ParticipantRow]
    capacity: int
    seats_taken: int


class ParticipantService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        locks: EventLocks,
        notifier: Notifier | None = None,
        post_refresher: PostRefresher | None = None,
        rules: RulesSettings | Callable[[], RulesSettings] | None = None,
        render_text: RenderText | None = None,
        *,
        ban_service: BanService | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._locks = locks
        self._notifier: Notifier = notifier or NullNotifier()
        self._refresher: PostRefresher = post_refresher or NullPostRefresher()
        self._rules = rules
        self._render: RenderText = render_text or default_render_text
        self._bans = ban_service

    async def admin_add(
        self,
        actor_id: int,
        event_id: int,
        user: UserInfo | None = None,
        manual_name: str | None = None,
        force: bool = False,
    ) -> AddResult:
        manual_name = (manual_name or "").strip() or None
        if (user is None) == (manual_name is None):
            raise InvalidOperation(ErrorCode.WHO_REQUIRED, "нужен либо пользователь, либо имя")
        now = self._clock.now()
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_event(session, actor_id, event_id)
            taken = await events.seats_taken(session, event_id)
            over = taken + 1 > event.capacity
            if over and not force:
                raise InvalidOperation(ErrorCode.OVER_CAPACITY)
            if user is not None:
                if await registrations.get_live(session, event_id, user.id) is not None:
                    raise InvalidOperation(ErrorCode.ALREADY_REGISTERED)
                await users.upsert(session, user, now)
            registration = Registration(
                event_id=event_id,
                user_id=user.id if user else None,
                manual_name=manual_name,
                guests=0,
                status=RegistrationStatus.ACTIVE.value,
                source=RegistrationSource.ADMIN.value,
                created_at=now,
            )
            session.add(registration)
            await session.flush()
            await audit.add(
                session,
                action="registration.admin_add",
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                target_user_id=user.id if user else None,
                payload={
                    "registration_id": registration.id,
                    "manual_name": manual_name,
                    "over_capacity": over,
                },
            )
            result = AddResult(registration.id, taken + 1, event.capacity, over)
        safe_schedule(self._refresher, event_id)
        return result

    async def admin_remove(
        self, actor_id: int, event_id: int, registration_id: int
    ) -> RemoveResult:
        """Удаляет запись без штрафа и освобождает место."""
        return await self._release(
            actor_id,
            event_id,
            registration_id,
            as_no_show=False,
            action="registration.admin_remove",
        )

    async def admin_cancel_after_deadline(
        self, actor_id: int, event_id: int, registration_id: int, as_no_show: bool
    ) -> RemoveResult:
        """Отмена после дедлайна: «без штрафа» (как удаление) или «как неявка».

        Если сервису передан `ban_service`, бан пересчитывается после коммита; иначе флаг
        `ban_recalc_required` в результате говорит вызывающему, что пересчёт нужен.
        """
        result = await self._release(
            actor_id,
            event_id,
            registration_id,
            as_no_show=as_no_show,
            action="registration.cancel_no_show" if as_no_show else "registration.admin_remove",
        )
        if not (result.ban_recalc_required and self._bans and result.user_id is not None):
            return result
        # Сбой пересчёта не должен терять уже сохранённую отмену: неявку учтёт отметка явки
        # этой встречи (`mark`), а её сбой дожмёт BanRecheckTask (`retry_pending_bans`)
        try:
            outcome = await self._bans.recalc_for_attendance(actor_id, result.user_id, event_id)
        except Exception:
            logger.exception("не удалось пересчитать бан (user_id=%s)", result.user_id)
            return result
        return replace(result, banned=outcome.banned)

    async def list_participants(self, actor_id: int, event_id: int) -> ParticipantList:
        async with self._sessions() as session:
            event = await self._load_event(session, actor_id, event_id, editable_only=False)
            rows = await events_admin.list_with_users(session, event_id, LIVE_REGISTRATION_STATUSES)
            active: list[ParticipantRow] = []
            waitlist: list[ParticipantRow] = []
            for registration, user in rows:
                is_waiting = registration.status == RegistrationStatus.WAITLIST.value
                row = ParticipantRow(
                    registration_id=registration.id,
                    user_id=registration.user_id,
                    name=display_name(user, registration.manual_name),
                    username=user.username if user else None,
                    guests=registration.guests,
                    status=registration.status,
                    source=registration.source,
                    attendance=registration.attendance,
                    created_at=registration.created_at,
                    position=len(waitlist) + 1 if is_waiting else None,
                    dm_available=user is not None and user.dm_available,
                )
                (waitlist if is_waiting else active).append(row)
            taken = await events.seats_taken(session, event_id)
            return ParticipantList(active, waitlist, event.capacity, taken)

    async def _release(
        self, actor_id: int, event_id: int, registration_id: int, *, as_no_show: bool, action: str
    ) -> RemoveResult:
        now = self._clock.now()
        late_hours = self._rules_settings().late_promotion_hours
        async with self._locks.lock(event_id), self._sessions() as session, session.begin():
            event = await self._load_event(session, actor_id, event_id)
            registration = await events_admin.get_registration(session, registration_id)
            if registration is None or registration.event_id != event_id:
                raise NotFound(f"запись {registration_id} не найдена")
            if registration.status not in LIVE_REGISTRATION_STATUSES:
                raise InvalidOperation(ErrorCode.NOT_LIVE)
            was_active = registration.status == RegistrationStatus.ACTIVE.value
            if as_no_show:
                if not was_active:
                    raise InvalidOperation(ErrorCode.NO_SHOW_REQUIRES_ACTIVE)
                registration.status = RegistrationStatus.CANCELLED.value
                registration.cancelled_at = now
                registration.attendance = Attendance.NO_SHOW.value
            else:
                registration.status = RegistrationStatus.REMOVED.value
                registration.removed_at = now
                registration.removed_by = actor_id
            await session.flush()
            promotions: list[Promotion] = []
            if was_active:
                promotions = await promote_waitlist(
                    session, event, now, late_promotion_hours=late_hours
                )
            await audit.add(
                session,
                action=action,
                now=now,
                actor_id=actor_id,
                event_id=event_id,
                target_user_id=registration.user_id,
                payload={
                    "registration_id": registration_id,
                    "as_no_show": as_no_show,
                    "promoted": [p.registration_id for p in promotions],
                },
            )
            result = RemoveResult(
                registration_id=registration_id,
                user_id=registration.user_id,
                promoted=tuple(promotions),
                ban_recalc_required=as_no_show and registration.user_id is not None,
            )
        safe_schedule(self._refresher, event_id)
        await notify_promotions(self._notifier, self._render, promotions)
        return result

    def _rules_settings(self) -> RulesSettings:
        return RulesSettings() if self._rules is None else resolve(self._rules)

    @staticmethod
    async def _load_event(
        session: AsyncSession, actor_id: int, event_id: int, *, editable_only: bool = True
    ) -> Event:
        await access.require_admin(session, actor_id)
        event = await events.get(session, event_id)
        if event is None:
            raise NotFound(f"встреча {event_id} не найдена")
        if editable_only and event.status not in UNFINISHED_EVENT_STATUSES:
            raise InvalidOperation(ErrorCode.BAD_STATUS)
        return event
