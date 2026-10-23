"""Запись на встречу и отмена записи (разделы 5.3 и 5.4 спецификации).

Сервис не знает про Telegram и возвращает значения-результаты; тексты подставляет handler.
Проверки записи идут строго в порядке спецификации, первая сработавшая определяет ответ.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.db.models import (
    EventStatus,
    Registration,
    RegistrationSource,
    RegistrationStatus,
)
from meetup_bot.repositories import audit, bans, events, registrations, users
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.ports import Notifier, NullNotifier, NullPostRefresher, PostRefresher
from meetup_bot.services.rules import RulesSettings
from meetup_bot.services.waitlist import Promotion, promote_waitlist

__all__ = [
    "CancelResult",
    "CancelStatus",
    "RegisterResult",
    "RegisterStatus",
    "RegistrationOptions",
    "RegistrationService",
    "UserInfo",
]


class RegisterStatus(enum.StrEnum):
    OK = "ok"
    ALREADY = "already"
    ALREADY_WAITLIST = "already_waitlist"
    WAITLISTED = "waitlisted"
    FULL = "full"
    ONLY_ONE_SEAT = "only_one_seat"
    CLOSED = "closed"
    BANNED = "banned"
    NOT_FOUND = "not_found"


class CancelStatus(enum.StrEnum):
    OK = "ok"
    NOT_REGISTERED = "not_registered"
    TOO_LATE = "too_late"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class RegisterResult:
    status: RegisterStatus
    seats_taken: int = 0
    capacity: int = 0
    position: int | None = None
    # Запись создана после дедлайна отмены: handler добавит предупреждение (`text.registered_late`)
    cancel_deadline_passed: bool = False
    # Эта запись заняла последнее свободное место (для уведомления админов)
    filled_event: bool = False

    @property
    def changed_state(self) -> bool:
        """Нужно ли пересобрать посты встречи."""
        return self.status in (RegisterStatus.OK, RegisterStatus.WAITLISTED)


@dataclass(frozen=True)
class CancelResult:
    status: CancelStatus
    seats_taken: int = 0
    capacity: int = 0
    was_waitlist: bool = False
    # Кого освобождённое место продвинуло из очереди (уже закоммичено)
    promoted: tuple[Promotion, ...] = ()

    @property
    def changed_state(self) -> bool:
        return self.status is CancelStatus.OK


@dataclass(frozen=True)
class RegistrationOptions:
    """Включаемые настройки правил; по умолчанию выключены (раздел 7)."""

    waitlist_enabled: bool = False
    plus_one_enabled: bool = False


PROMOTED_DM_TEXT = "Освободилось место — вы записаны на встречу!"


class RegistrationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        clock: Clock,
        options: RegistrationOptions | None = None,
        *,
        locks: EventLocks | None = None,
        notifier: Notifier | None = None,
        post_refresher: PostRefresher | None = None,
        rules_provider: Callable[[], RulesSettings] | None = None,
        options_provider: Callable[[], RegistrationOptions] | None = None,
        render_promoted: Callable[[Promotion], str] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._options = options or RegistrationOptions()
        # Реестр общий для всех сервисов: процесс один, блокировок в памяти достаточно (9.3)
        self._locks = locks or EventLocks()
        self._notifier: Notifier = notifier or NullNotifier()
        self._post_refresher: PostRefresher = post_refresher or NullPostRefresher()
        self._rules_provider: Callable[[], RulesSettings] = rules_provider or RulesSettings
        self._options_provider = options_provider
        self._render_promoted = render_promoted

    def _current_options(self) -> RegistrationOptions:
        return self._options_provider() if self._options_provider else self._options

    async def register(self, event_id: int, user: UserInfo, guests: int = 0) -> RegisterResult:
        options = self._current_options()
        if guests not in (0, 1):
            raise ValueError("guests должен быть 0 или 1")
        if guests and not options.plus_one_enabled:
            raise ValueError("запись с гостем выключена настройкой plus_one_enabled")

        # Блокировка + BEGIN IMMEDIATE (в движке) + уникальный частичный индекс: три линии защиты
        async with (
            self._locks.lock(event_id),
            self._sessions() as session,
            session.begin(),
        ):
            result = await self._register(session, event_id, user, guests, options)

        # Побочные эффекты — строго после коммита и вне блокировки
        if result.changed_state:
            self._post_refresher.schedule(event_id)
        if result.filled_event and self._rules_provider().notify_admins_on_full:
            await self._notifier.send_service(
                f"Встреча заполнена: занято {result.seats_taken} из {result.capacity} мест."
            )
        return result

    async def cancel(self, event_id: int, user_id: int) -> CancelResult:
        options = self._current_options()
        rules = self._rules_provider()
        async with (
            self._locks.lock(event_id),
            self._sessions() as session,
            session.begin(),
        ):
            result = await self._cancel(session, event_id, user_id, options, rules)

        if not result.changed_state:
            return result
        self._post_refresher.schedule(event_id)
        for promotion in result.promoted:
            if promotion.user_id is not None:
                text = (
                    self._render_promoted(promotion) if self._render_promoted else PROMOTED_DM_TEXT
                )
                await self._notifier.send_dm(promotion.user_id, text)
        # Выход из очереди мест не освобождает, поэтому админам о нём не сообщаем
        if rules.notify_admins_on_cancel and not result.was_waitlist:
            await self._notifier.send_service(
                f"Отмена записи: пользователь {user_id}, занято "
                f"{result.seats_taken} из {result.capacity} мест."
            )
        return result

    async def _register(
        self,
        session: AsyncSession,
        event_id: int,
        user: UserInfo,
        guests: int,
        options: RegistrationOptions,
    ) -> RegisterResult:
        now = self._clock.now()
        event = await events.get(session, event_id)
        if event is None:
            return RegisterResult(RegisterStatus.NOT_FOUND)

        taken = await events.seats_taken(session, event_id)

        def result(
            status: RegisterStatus,
            *,
            position: int | None = None,
            cancel_deadline_passed: bool = False,
            filled_event: bool = False,
        ) -> RegisterResult:
            return RegisterResult(
                status,
                seats_taken=taken,
                capacity=event.capacity,
                position=position,
                cancel_deadline_passed=cancel_deadline_passed,
                filled_event=filled_event,
            )

        if event.status != EventStatus.OPEN.value or now >= event.registration_closes_at:
            return result(RegisterStatus.CLOSED)
        if await bans.is_banned(session, user.id):
            return result(RegisterStatus.BANNED)

        existing = await registrations.get_live(session, event_id, user.id)
        if existing is not None:
            if existing.status == RegistrationStatus.ACTIVE.value:
                return result(RegisterStatus.ALREADY)
            position = await registrations.waitlist_position(session, existing)
            return result(RegisterStatus.ALREADY_WAITLIST, position=position)

        requested = 1 + guests
        free = event.capacity - taken
        if requested > free:
            if options.waitlist_enabled:
                registration = await self._add(
                    session, event_id, user, guests, RegistrationStatus.WAITLIST
                )
                position = await registrations.waitlist_position(session, registration)
                await audit.add(
                    session,
                    action="registration.waitlist",
                    now=now,
                    actor_id=user.id,
                    event_id=event_id,
                    target_user_id=user.id,
                    payload={"position": position},
                )
                return result(RegisterStatus.WAITLISTED, position=position)
            if guests and free == 1:
                return result(RegisterStatus.ONLY_ONE_SEAT)
            return result(RegisterStatus.FULL)

        await self._add(session, event_id, user, guests, RegistrationStatus.ACTIVE)
        taken += requested
        await audit.add(
            session,
            action="registration.create",
            now=now,
            actor_id=user.id,
            event_id=event_id,
            target_user_id=user.id,
            payload={"guests": guests, "seats_taken": taken},
        )
        return result(
            RegisterStatus.OK,
            cancel_deadline_passed=now >= event.cancel_deadline_at,
            filled_event=taken >= event.capacity,
        )

    async def _add(
        self,
        session: AsyncSession,
        event_id: int,
        user: UserInfo,
        guests: int,
        status: RegistrationStatus,
    ) -> Registration:
        now = self._clock.now()
        await users.upsert(session, user, now)
        registration = Registration(
            event_id=event_id,
            user_id=user.id,
            guests=guests,
            status=status.value,
            source=RegistrationSource.BUTTON.value,
            created_at=now,
        )
        session.add(registration)
        await session.flush()
        return registration

    async def _cancel(
        self,
        session: AsyncSession,
        event_id: int,
        user_id: int,
        options: RegistrationOptions,
        rules: RulesSettings,
    ) -> CancelResult:
        now = self._clock.now()
        event = await events.get(session, event_id)
        if event is None:
            return CancelResult(CancelStatus.NOT_FOUND)
        taken = await events.seats_taken(session, event_id)

        registration = await registrations.get_live(session, event_id, user_id)
        if registration is None:
            return CancelResult(CancelStatus.NOT_REGISTERED, taken, event.capacity)

        is_open = event.status == EventStatus.OPEN.value
        was_waitlist = registration.status == RegistrationStatus.WAITLIST.value
        # Из очереди можно выйти в любой момент до закрытия записи, без штрафа
        deadline_ok = (
            now < event.registration_closes_at if was_waitlist else now < event.cancel_deadline_at
        )
        if not is_open or not deadline_ok:
            return CancelResult(CancelStatus.TOO_LATE, taken, event.capacity, was_waitlist)

        registration.status = RegistrationStatus.CANCELLED.value
        registration.cancelled_at = now
        await session.flush()
        await audit.add(
            session,
            action="registration.cancel",
            now=now,
            actor_id=user_id,
            event_id=event_id,
            target_user_id=user_id,
            payload={"was_waitlist": was_waitlist},
        )
        promoted: list[Promotion] = []
        # Место освобождает только отмена активной записи, и только при включённой очереди
        if not was_waitlist and options.waitlist_enabled:
            promoted = await promote_waitlist(
                session, event, now, late_promotion_hours=rules.late_promotion_hours
            )
        taken = await events.seats_taken(session, event_id)
        return CancelResult(CancelStatus.OK, taken, event.capacity, was_waitlist, tuple(promoted))
