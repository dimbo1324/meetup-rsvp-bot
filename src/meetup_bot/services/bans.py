"""Баны: ручные и автоматические по неявкам (раздел 5.8)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import (
    Ban,
    BanReason,
    Event,
    EventStatus,
    Registration,
    RegistrationStatus,
    User,
)
from meetup_bot.repositories import admins, audit
from meetup_bot.repositories import bans as bans_repo
from meetup_bot.services.errors import NotFound
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.ports import Notifier, PostRefresher
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.waitlist import promote_waitlist

BAN_DM_TEXT = "Вы заблокированы и не можете записываться на встречи."

# Повторы на случай, когда между поиском будущих встреч и блокировкой пользователь записался
_MAX_LOCK_ATTEMPTS = 5

_LIVE = (RegistrationStatus.ACTIVE.value, RegistrationStatus.WAITLIST.value)


@dataclass(frozen=True)
class BanOutcome:
    """Итог пересчёта бана: выдан, снят или без изменений."""

    ban: Ban | None
    banned: bool = False
    unbanned: bool = False


class BanService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        locks: EventLocks,
        notifier: Notifier,
        post_refresher: PostRefresher,
        rules_provider: Callable[[], RulesSettings] = RulesSettings,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._locks = locks
        self._notifier = notifier
        self._refresher = post_refresher
        self._rules = rules_provider

    async def ban(
        self,
        actor_id: int,
        user_id: int,
        reason: BanReason = BanReason.MANUAL,
        comment: str | None = None,
        event_id: int | None = None,
    ) -> Ban:
        """Идемпотентно: если пользователь уже забанен, возвращается действующий бан."""
        async with self._sessions() as session, session.begin():
            await admins.require_admin(session, actor_id)
        ban, created = await self._ban_user(actor_id, user_id, reason, comment, event_id)
        if created and self._rules().notify_user_on_ban:
            await self._notifier.send_dm(user_id, BAN_DM_TEXT)
        return ban

    async def unban(self, actor_id: int, user_id: int) -> Ban | None:
        """Снимает действующий бан; None, если бана не было."""
        now = self._clock.now()
        async with self._sessions() as session, session.begin():
            await admins.require_admin(session, actor_id)
            ban = await bans_repo.get_active(session, user_id)
            if ban is None:
                return None
            self._deactivate(ban, actor_id, now)
            await self._audit_unban(session, ban, actor_id, now, None)
            return ban

    async def list_banned(self, actor_id: int) -> list[Ban]:
        async with self._sessions() as session, session.begin():
            await admins.require_admin(session, actor_id)
            return await bans_repo.list_active(session)

    async def history(self, actor_id: int, user_id: int) -> list[Ban]:
        async with self._sessions() as session, session.begin():
            await admins.require_admin(session, actor_id)
            return await bans_repo.history(session, user_id)

    async def evaluate_no_show_ban(self, actor_id: int, user_id: int, event_id: int) -> BanOutcome:
        """Пересчитывает автобан по правилу 5.8 после отметки или исправления явки.

        Права не проверяются: вызывается из AttendanceService, который уже проверил админа.
        Снимается только бан, выданный за неявку именно на этой встрече; ручные не трогаем.
        """
        threshold = self._rules().ban_after_no_shows
        async with self._sessions() as session, session.begin():
            count = await bans_repo.count_no_shows(session, user_id)
            active = await bans_repo.get_active(session, user_id)
            if (
                threshold > 0
                and count < threshold
                and active is not None
                and active.reason == BanReason.NO_SHOW.value
                and active.event_id == event_id
            ):
                now = self._clock.now()
                self._deactivate(active, actor_id, now)
                await self._audit_unban(
                    session, active, actor_id, now, "автобан снят: отметка явки исправлена"
                )
                return BanOutcome(active, unbanned=True)
        if threshold > 0 and count >= threshold and active is None:
            ban, created = await self._ban_user(
                actor_id, user_id, BanReason.NO_SHOW, None, event_id
            )
            if created and self._rules().notify_user_on_ban:
                await self._notifier.send_dm(user_id, BAN_DM_TEXT)
            return BanOutcome(ban, banned=created)
        return BanOutcome(active)

    async def _ban_user(
        self,
        actor_id: int,
        user_id: int,
        reason: BanReason,
        comment: str | None,
        event_id: int | None,
    ) -> tuple[Ban, bool]:
        """Создаёт бан и снимает записи с будущих встреч под их блокировками.

        Блокировки берутся по возрастанию id (единый порядок исключает взаимную блокировку)
        и строго до открытия транзакции, как в остальных сервисах.
        """
        remove = self._rules().remove_banned_from_future_events
        for _ in range(_MAX_LOCK_ATTEMPTS):
            candidates: set[int] = set()
            if remove:
                async with self._sessions() as session, session.begin():
                    candidates = await self._future_event_ids(session, user_id)
            async with AsyncExitStack() as stack:
                for locked_id in sorted(candidates):
                    await stack.enter_async_context(self._locks.lock(locked_id))
                async with self._sessions() as session, session.begin():
                    now = self._clock.now()
                    if await session.get(User, user_id) is None:
                        raise NotFound(f"пользователь {user_id} не найден")
                    existing = await bans_repo.get_active(session, user_id)
                    if existing is not None:
                        return existing, False
                    fresh = await self._future_event_ids(session, user_id) if remove else set()
                    if not fresh <= candidates:
                        continue
                    ban = Ban(
                        user_id=user_id,
                        active=True,
                        reason=reason.value,
                        comment=comment,
                        event_id=event_id,
                        banned_by=actor_id,
                        banned_at=now,
                    )
                    session.add(ban)
                    await session.flush()
                    touched = await self._remove_from_events(session, user_id, fresh, actor_id, now)
                    await audit.add(
                        session,
                        action="ban.create",
                        now=now,
                        actor_id=actor_id,
                        event_id=event_id,
                        target_user_id=user_id,
                        payload={
                            "reason": reason.value,
                            "comment": comment,
                            "removed_from_events": sorted(touched),
                        },
                    )
            for touched_id in sorted(touched):
                self._refresher.schedule(touched_id)
            return ban, True
        raise RuntimeError("не удалось взять блокировки встреч для бана")

    async def _future_event_ids(self, session: AsyncSession, user_id: int) -> set[int]:
        rows = await session.scalars(
            select(Registration.event_id)
            .join(Event, Registration.event_id == Event.id)
            .where(
                Registration.user_id == user_id,
                Registration.status.in_(_LIVE),
                Event.status.in_((EventStatus.OPEN.value, EventStatus.CLOSED.value)),
                Event.starts_at > self._clock.now(),
            )
        )
        return set(rows.all())

    async def _remove_from_events(
        self,
        session: AsyncSession,
        user_id: int,
        event_ids: set[int],
        actor_id: int,
        now: datetime,
    ) -> set[int]:
        """Снимает записи и очередь; возвращает встречи, у которых что-то изменилось."""
        touched: set[int] = set()
        late_hours = self._rules().late_promotion_hours
        for event_id in sorted(event_ids):
            rows = await session.scalars(
                select(Registration).where(
                    Registration.event_id == event_id,
                    Registration.user_id == user_id,
                    Registration.status.in_(_LIVE),
                )
            )
            freed_seat = False
            for registration in rows.all():
                if registration.status == RegistrationStatus.ACTIVE.value:
                    freed_seat = True
                registration.status = RegistrationStatus.REMOVED.value
                registration.removed_by = actor_id
                registration.removed_at = now
                touched.add(event_id)
            await session.flush()
            event = await session.get(Event, event_id)
            if freed_seat and event is not None:
                await promote_waitlist(session, event, now, late_promotion_hours=late_hours)
        return touched

    @staticmethod
    def _deactivate(ban: Ban, actor_id: int, now: datetime) -> None:
        ban.active = False
        ban.unbanned_by = actor_id
        ban.unbanned_at = now

    @staticmethod
    async def _audit_unban(
        session: AsyncSession, ban: Ban, actor_id: int, now: datetime, comment: str | None
    ) -> None:
        await audit.add(
            session,
            action="ban.remove",
            now=now,
            actor_id=actor_id,
            event_id=ban.event_id,
            target_user_id=ban.user_id,
            payload={"ban_id": ban.id, "comment": comment},
        )
