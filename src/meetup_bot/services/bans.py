"""Баны: ручные и автоматические по неявкам (раздел 5.8)."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import Ban, BanReason, RegistrationStatus
from meetup_bot.repositories import admins, audit, events, registrations
from meetup_bot.repositories import bans as bans_repo
from meetup_bot.repositories import users as users_repo
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services import access
from meetup_bot.services.admin_support import (
    Provided,
    RenderText,
    default_render_text,
    notify_promotions,
)
from meetup_bot.services.errors import ErrorCode, InvalidOperation
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.ports import Notifier, PostRefresher
from meetup_bot.services.provided import resolve_or_default
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.safe import safe_schedule, safe_send_dm
from meetup_bot.services.waitlist import Promotion, promote_waitlist

# Повторы на случай, когда между поиском будущих встреч и блокировкой пользователь записался
_MAX_LOCK_ATTEMPTS = 5


@dataclass(frozen=True)
class BanOutcome:
    """Итог пересчёта бана: выдан, снят или без изменений."""

    ban: Ban | None
    banned: bool = False
    unbanned: bool = False


@dataclass(frozen=True)
class _Created:
    """Итог создания бана; побочные эффекты к этому моменту уже выполнены."""

    ban: Ban
    created: bool


class BanService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        locks: EventLocks,
        notifier: Notifier,
        post_refresher: PostRefresher,
        rules_provider: Provided[RulesSettings] | None = None,
        render_text: RenderText | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._locks = locks
        self._notifier = notifier
        self._refresher = post_refresher
        self._rules = rules_provider
        self._render: RenderText = render_text or default_render_text

    def _rules_settings(self) -> RulesSettings:
        return resolve_or_default(self._rules, RulesSettings)

    async def ban(
        self,
        actor_id: int,
        user_id: int,
        reason: BanReason = BanReason.MANUAL,
        comment: str | None = None,
        event_id: int | None = None,
        user: UserInfo | None = None,
    ) -> Ban:
        """Идемпотентно: если пользователь уже забанен, возвращается действующий бан.

        Забанить можно того, кто ещё не писал боту: бан привязан к Telegram ID. Если админ
        выбрал человека из Telegram, `user` сохраняет его имя для чёрного списка.
        """
        if user is not None and user.id != user_id:
            raise InvalidOperation(ErrorCode.INVALID_USER, "данные пользователя не совпадают с ID")
        if user_id <= 0:
            raise InvalidOperation(ErrorCode.INVALID_USER, "некорректный Telegram ID")
        async with self._sessions() as session, session.begin():
            await access.require_admin(session, actor_id)
            # Владельца, админов и себя банить нельзя: иначе можно отрезать доступ к управлению
            if reason is BanReason.MANUAL and await admins.is_admin(session, user_id):
                raise InvalidOperation(
                    ErrorCode.CANNOT_BAN_ADMIN, "нельзя забанить админа или владельца"
                )
        created = await self._ban_user(actor_id, user_id, reason, comment, event_id, user)
        return created.ban

    async def unban(self, actor_id: int, user_id: int) -> Ban | None:
        """Снимает действующий бан; None, если бана не было."""
        now = self._clock.now()
        async with self._sessions() as session, session.begin():
            await access.require_admin(session, actor_id)
            ban = await bans_repo.get_active(session, user_id)
            if ban is None:
                return None
            self._deactivate(ban, actor_id, now, auto=False)
            await self._audit_unban(session, ban, actor_id, now, None)
            return ban

    async def list_banned(self, actor_id: int) -> list[Ban]:
        async with self._sessions() as session, session.begin():
            await access.require_admin(session, actor_id)
            return await bans_repo.list_active(session)

    async def history(self, actor_id: int, user_id: int) -> list[Ban]:
        async with self._sessions() as session, session.begin():
            await access.require_admin(session, actor_id)
            return await bans_repo.history(session, user_id)

    async def recalc_for_attendance(
        self, actor_id: int | None, user_id: int, event_id: int
    ) -> BanOutcome:
        """Внутренний интерфейс сервисов: пересчёт автобана после отметки/исправления явки.

        Права здесь не проверяются, их проверяет вызывающий сервис; `actor_id` нужен для аудита.
        """
        return await self._evaluate_no_show_ban(actor_id, user_id, event_id)

    async def _evaluate_no_show_ban(
        self, actor_id: int | None, user_id: int, event_id: int
    ) -> BanOutcome:
        """Пересчитывает автобан по правилу 5.8.

        Автобан снимается, как только неявок стало меньше порога, — по какой бы встрече он
        ни был выдан: при пороге 2 исправление любой из двух неявок убирает основание бана.
        Ручные баны не трогаем. Автоснятие помечается `auto_removed`, чтобы не обнулять
        счётчик неявок как ручной разбан.
        """
        threshold = self._rules_settings().ban_after_no_shows
        async with self._sessions() as session, session.begin():
            count = await bans_repo.count_no_shows(session, user_id)
            active = await bans_repo.get_active(session, user_id)
            if (
                threshold > 0
                and count < threshold
                and active is not None
                and active.reason == BanReason.NO_SHOW.value
            ):
                now = self._clock.now()
                self._deactivate(active, actor_id, now, auto=True)
                await self._audit_unban(
                    session, active, actor_id, now, "автобан снят: отметка явки исправлена"
                )
                return BanOutcome(active, unbanned=True)
        if threshold > 0 and count >= threshold and active is None:
            created = await self._ban_user(actor_id, user_id, BanReason.NO_SHOW, None, event_id)
            return BanOutcome(created.ban, banned=created.created)
        return BanOutcome(active)

    async def _ban_user(
        self,
        actor_id: int | None,
        user_id: int,
        reason: BanReason,
        comment: str | None,
        event_id: int | None,
        user: UserInfo | None = None,
    ) -> _Created:
        """Создаёт бан и снимает записи с будущих встреч под их блокировками.

        Блокировки берутся по возрастанию id (единый порядок исключает взаимную блокировку)
        и строго до открытия транзакции, как в остальных сервисах. Побочные эффекты
        (посты, сообщения продвинутым и забаненному) выполняются после коммита.
        """
        remove = self._rules_settings().remove_banned_from_future_events
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
                    existing = await bans_repo.get_active(session, user_id)
                    if existing is not None:
                        return _Created(existing, False)
                    fresh = await self._future_event_ids(session, user_id) if remove else set()
                    if not fresh <= candidates:
                        continue
                    # Имя выбранного админом человека или заглушка для внешнего ключа на users
                    if user is not None:
                        await users_repo.upsert(session, user, now)
                    else:
                        await users_repo.ensure_exists(session, user_id, now)
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
                    touched, promoted = await self._remove_from_events(
                        session, user_id, fresh, actor_id, now
                    )
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
            await self._after_ban(user_id, touched, promoted)
            return _Created(ban, True)
        raise RuntimeError("не удалось взять блокировки встреч для бана")

    async def _after_ban(self, user_id: int, touched: set[int], promoted: list[Promotion]) -> None:
        """После коммита: сбой Telegram не должен терять уже выданный бан."""
        for touched_id in sorted(touched):
            safe_schedule(self._refresher, touched_id)
        await notify_promotions(self._notifier, self._render, promoted)
        if self._rules_settings().notify_user_on_ban:
            await safe_send_dm(self._notifier, user_id, self._render("ban_notice", {}))

    async def _future_event_ids(self, session: AsyncSession, user_id: int) -> set[int]:
        return await registrations.future_event_ids_of_user(session, user_id, self._clock.now())

    async def _remove_from_events(
        self,
        session: AsyncSession,
        user_id: int,
        event_ids: set[int],
        actor_id: int | None,
        now: datetime,
    ) -> tuple[set[int], list[Promotion]]:
        """Снимает записи и очередь; возвращает изменившиеся встречи и продвинутых из очереди."""
        touched: set[int] = set()
        promoted: list[Promotion] = []
        late_hours = self._rules_settings().late_promotion_hours
        for event_id in sorted(event_ids):
            freed_seat = False
            for registration in await registrations.list_live_of_user(session, event_id, user_id):
                if registration.status == RegistrationStatus.ACTIVE.value:
                    freed_seat = True
                registration.status = RegistrationStatus.REMOVED.value
                registration.removed_by = actor_id
                registration.removed_at = now
                touched.add(event_id)
            await session.flush()
            event = await events.get(session, event_id)
            if freed_seat and event is not None:
                promoted.extend(
                    await promote_waitlist(session, event, now, late_promotion_hours=late_hours)
                )
        return touched, promoted

    @staticmethod
    def _deactivate(ban: Ban, actor_id: int | None, now: datetime, *, auto: bool) -> None:
        ban.active = False
        ban.auto_removed = auto
        ban.unbanned_by = actor_id
        ban.unbanned_at = now

    @staticmethod
    async def _audit_unban(
        session: AsyncSession, ban: Ban, actor_id: int | None, now: datetime, comment: str | None
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
