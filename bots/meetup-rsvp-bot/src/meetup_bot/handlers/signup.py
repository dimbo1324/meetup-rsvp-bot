"""Нажатия кнопок под постом: записаться, записаться с гостем, отменить запись."""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.container import Services
from meetup_bot.renderer.post import (
    ACTION_CANCEL,
    ACTION_JOIN,
    ACTION_JOIN_PLUS_ONE,
    SIGNUP_PREFIX,
    SIGNUP_SEP,
)
from meetup_bot.services.errors import DomainError, ErrorCode, InvalidOperation
from meetup_bot.services.posts import register_post
from meetup_bot.services.registration import (
    CancelResult,
    CancelStatus,
    RegisterResult,
    RegisterStatus,
)
from meetup_bot.telegram.callbacks import SignupCallback
from meetup_bot.telegram.common import (
    ALERT_LIMIT,
    GENERIC_ERROR_TEXT,
    START_REMIND,
    safe_answer,
    telegram_length,
)
from meetup_bot.telegram.post_updater import PostUpdater
from meetup_bot.texts.render import text_variables
from meetup_bot.user_info import UserInfo

logger = logging.getLogger(__name__)

router = Router(name="signup")

_REGISTER_TEXT_KEYS = {
    RegisterStatus.ALREADY: "text.already",
    RegisterStatus.ALREADY_WAITLIST: "text.already_waitlist",
    RegisterStatus.WAITLISTED: "text.waitlisted",
    RegisterStatus.FULL: "text.full",
    RegisterStatus.ONLY_ONE_SEAT: "text.only_one_seat",
    RegisterStatus.CLOSED: "text.closed",
    RegisterStatus.BANNED: "text.banned",
}
_CANCEL_TEXT_KEYS = {
    CancelStatus.OK: "text.cancelled",
    CancelStatus.NOT_REGISTERED: "text.not_registered",
    CancelStatus.TOO_LATE: "text.cancel_too_late",
}
NOT_FOUND_TEXT = "Встреча не найдена"
NO_GUESTS_TEXT = "Запись с гостем сейчас недоступна."


@router.callback_query(SignupCallback.filter())
async def on_signup_callback(
    callback: CallbackQuery,
    callback_data: SignupCallback,
    user_info: UserInfo,
    services: Services,
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    post_updater: PostUpdater,
    bot_username: str,
) -> None:
    """Всегда отвечает на callback: иначе у пользователя бесконечно крутятся «часики»."""
    text = GENERIC_ERROR_TEXT
    url: str | None = None
    event_id = callback_data.event_id
    # Ленивая регистрация поста — только для записи: отмена от чужого или поддельного
    # callback не должна заводить пост, его регистрирует chosen_inline_result
    register_post_lazily = False
    try:
        action = callback_data.action
        if action in (ACTION_JOIN, ACTION_JOIN_PLUS_ONE):
            guests = 1 if action == ACTION_JOIN_PLUS_ONE else 0
            register_result = await services.registration.register(event_id, user_info, guests)
            text = _register_text(services, register_result)
            if (
                register_result.status in (RegisterStatus.OK, RegisterStatus.WAITLISTED)
                and not register_result.dm_available
            ):
                hint = services.settings.render_text_now("text.enable_dm_hint", escape_html=False)
                # Подсказку добавляем только если она целиком влезает во всплывающее окно
                combined = f"{text}\n{hint}"
                if telegram_length(combined) <= ALERT_LIMIT:
                    text = combined
                    url = f"https://t.me/{bot_username}?start={START_REMIND}"
            known_event = register_result.status is not RegisterStatus.NOT_FOUND
            register_post_lazily = known_event
        elif action == ACTION_CANCEL:
            cancel_result = await services.registration.cancel(event_id, user_info.id)
            text = _cancel_text(services, cancel_result)
            known_event = cancel_result.status is not CancelStatus.NOT_FOUND
        else:
            logger.warning("неизвестное действие кнопки: %r", action)
            known_event = False
        if known_event:
            if register_post_lazily:
                await _register_inline_post(callback, event_id, sessions, clock)
            # Даже если состояние не изменилось: пост мог устареть (5.11), а хеш отсечёт лишнюю
            # правку. Сервис уже ставит пересборку при изменениях; повтор безопасен (debounce).
            post_updater.schedule(event_id)
    except InvalidOperation as error:
        # Кнопка «+1» осталась под старым постом, а настройку уже выключили
        text = NO_GUESTS_TEXT if error.code == ErrorCode.PLUS_ONE_DISABLED else GENERIC_ERROR_TEXT
        logger.info("отказ сервиса записи для встречи %s: %s", event_id, error.code)
    except DomainError:
        logger.warning("отказ при обработке кнопки, встреча %s", event_id, exc_info=True)
        text = GENERIC_ERROR_TEXT
    except Exception:
        logger.exception("ошибка при обработке кнопки, встреча %s", event_id)
        text = GENERIC_ERROR_TEXT
    await safe_answer(callback, text, url=url)


async def _register_inline_post(
    callback: CallbackQuery,
    event_id: int,
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    """Подстраховка на случай, если `chosen_inline_result` не пришёл (8.1)."""
    inline_message_id = callback.inline_message_id
    if inline_message_id is None:
        return
    try:
        await register_post(
            sessions,
            clock,
            event_id=event_id,
            inline_message_id=inline_message_id,
            posted_by=None,
            has_photo=None,
        )
    except Exception:
        # Ответ пользователю важнее: пост зарегистрируется при следующем нажатии
        logger.exception("не удалось зарегистрировать пост встречи %s", event_id)


def _when_vars(
    services: Services,
    starts_at: datetime | None,
    ends_at: datetime | None,
    deadline_at: datetime | None,
    place: str,
) -> dict[str, str]:
    params = {
        "starts_at": starts_at,
        "ends_at": ends_at,
        "deadline_at": deadline_at,
        "place": place,
    }
    return text_variables(params, services.settings.timezone_now())


def _register_text(services: Services, result: RegisterResult) -> str:
    render = services.settings.render_text_now
    if result.status is RegisterStatus.NOT_FOUND:
        return NOT_FOUND_TEXT
    if result.status is RegisterStatus.OK:
        key = "text.registered_late" if result.cancel_deadline_passed else "text.registered"
    else:
        key = _REGISTER_TEXT_KEYS[result.status]
    variables = _when_vars(
        services, result.starts_at, result.ends_at, result.cancel_deadline_at, result.place
    )
    return render(key, escape_html=False, position=result.position or "", **variables)


def _cancel_text(services: Services, result: CancelResult) -> str:
    if result.status is CancelStatus.NOT_FOUND:
        return NOT_FOUND_TEXT
    variables = _when_vars(services, result.starts_at, result.ends_at, None, result.place)
    return services.settings.render_text_now(
        _CANCEL_TEXT_KEYS[result.status], escape_html=False, **variables
    )


@router.callback_query(F.data.startswith(SIGNUP_PREFIX + SIGNUP_SEP))
async def on_stale_callback(callback: CallbackQuery) -> None:
    """Кнопка старого или неизвестного формата: без ответа у пользователя крутятся часики."""
    await safe_answer(callback, "Эта кнопка устарела. Откройте свежий пост встречи.")
