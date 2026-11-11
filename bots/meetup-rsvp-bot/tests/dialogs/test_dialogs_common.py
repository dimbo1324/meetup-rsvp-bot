"""Общие помощники диалогов: проверка прав, определение нажавшего, данные middleware."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Message

from meetup_bot.dialogs._common import (
    actor_id,
    error_text,
    get_attendance_ui,
    get_bot,
    get_clock,
    get_services,
    guard,
    is_admin,
    notice_block,
    picked_user,
    pop_notice,
    set_notice,
)
from meetup_bot.services.errors import ErrorCode, InvalidOperation, NotFound, PermissionDenied
from meetup_bot.telegram.errors_text import ERROR_TEXTS, NO_ACCESS, describe_error
from meetup_bot.user_info import UserInfo
from tests.helpers import ADMIN_ID, OWNER_ID, App


def _manager(app: App, user_id: int) -> Any:
    """Минимальная подмена DialogManager: хватает middleware_data."""
    return SimpleNamespace(
        middleware_data={
            "services": app.services,
            "sessions": app.sessions,
            "event_from_user": SimpleNamespace(id=user_id),
            "clock": app.clock,
        }
    )


def test_actor_id_берёт_пользователя_а_не_чат(app: App) -> None:
    assert actor_id(_manager(app, 42)) == 42


async def test_is_admin_владелец_и_админ_допущены(app: App) -> None:
    assert await is_admin(_manager(app, OWNER_ID)) is True
    assert await is_admin(_manager(app, ADMIN_ID)) is True


async def test_is_admin_посторонний_не_допущен(app: App) -> None:
    assert await is_admin(_manager(app, 777)) is False


async def test_is_admin_проверяет_права_заново_после_снятия(app: App) -> None:
    manager = _manager(app, ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    assert await is_admin(manager) is False


def test_get_services_читает_middleware_data(app: App) -> None:
    assert get_services(_manager(app, OWNER_ID)) is app.services


# ---- типизированные getters ----------------------------------------------------------


def test_get_clock_bot_и_attendance_ui_читают_middleware_data() -> None:
    clock, bot, ui = object(), object(), object()
    manager: Any = SimpleNamespace(
        middleware_data={"clock": clock, "bot": bot, "attendance_ui": ui}
    )
    assert get_clock(manager) is clock
    assert get_bot(manager) is bot
    assert get_attendance_ui(manager) is ui


# ---- guard ---------------------------------------------------------------------------


def _callback() -> Any:
    return SimpleNamespace(answer=AsyncMock())


def _closable(app: App, user_id: int) -> Any:
    manager = _manager(app, user_id)
    manager.done = AsyncMock()
    return manager


async def test_guard_пускает_админа_и_ничего_не_показывает(app: App) -> None:
    manager = _closable(app, OWNER_ID)
    event = _callback()
    assert await guard(event, manager, close=True) is True
    event.answer.assert_not_awaited()
    manager.done.assert_not_awaited()


async def test_guard_отказывает_постороннему_всплывающим_окном_и_закрывает_диалог(
    app: App,
) -> None:
    manager = _closable(app, 777)
    callback = CallbackQuery.model_construct(id="1", from_user=None, chat_instance="c")
    answer = AsyncMock()
    object.__setattr__(callback, "answer", answer)
    assert await guard(callback, manager, close=True) is False
    answer.assert_awaited_once_with(NO_ACCESS, show_alert=True)
    manager.done.assert_awaited_once()


async def test_guard_без_close_диалог_не_закрывает(app: App) -> None:
    manager = _closable(app, 777)
    message = Message.model_construct(message_id=1)
    answer = AsyncMock()
    object.__setattr__(message, "answer", answer)
    assert await guard(message, manager, close=False) is False
    answer.assert_awaited_once_with(NO_ACCESS)
    manager.done.assert_not_awaited()


# ---- уведомление ---------------------------------------------------------------------


def _dialog() -> Any:
    return SimpleNamespace(dialog_data={})


def test_notice_block_экранирует_и_не_забывает_уведомление() -> None:
    manager = _dialog()
    assert notice_block(manager) == ""
    set_notice(manager, "a < b")
    assert notice_block(manager, "⚠️ ") == "\n⚠️ a &lt; b\n"
    assert notice_block(manager) == "\na &lt; b\n"


def test_pop_notice_возвращает_и_забывает() -> None:
    manager = _dialog()
    set_notice(manager, "готово")
    assert pop_notice(manager) == "готово"
    assert pop_notice(manager) == ""


def test_set_notice_none_снимает_уведомление() -> None:
    manager = _dialog()
    set_notice(manager, "x")
    set_notice(manager, None)
    assert notice_block(manager) == ""


# ---- тексты ошибок -------------------------------------------------------------------


def test_error_text_без_уточнения_только_текст_кода() -> None:
    assert error_text(InvalidOperation(ErrorCode.NOT_LIVE)) == ERROR_TEXTS[ErrorCode.NOT_LIVE]


def test_error_text_добавляет_уточнение_сервиса() -> None:
    exc = InvalidOperation(ErrorCode.INVALID_PLACE_URL, "Ссылка не длиннее 500 символов.")
    assert error_text(exc) == (
        f"{ERROR_TEXTS[ErrorCode.INVALID_PLACE_URL]} Ссылка не длиннее 500 символов."
    )


def test_error_text_не_повторяет_уточнение_уже_сказанное_в_тексте_кода() -> None:
    base = ERROR_TEXTS[ErrorCode.EVENT_CANCELLED]
    assert error_text(InvalidOperation(ErrorCode.EVENT_CANCELLED, base.rstrip("."))) == base


def test_error_text_для_прав_и_ненайденного_общие_тексты() -> None:
    assert error_text(PermissionDenied("не админ")) == NO_ACCESS
    assert error_text(NotFound("встреча 5 не найдена")) == describe_error(NotFound())


# ---- выбор пользователя --------------------------------------------------------------


def test_picked_user_собирает_userinfo_из_результата_выбора() -> None:
    result = {"user_id": 5, "first_name": "Борис", "last_name": "К", "username": "bk"}
    assert picked_user(result) == UserInfo(5, "Борис", "К", "bk")


def test_picked_user_пустое_имя_допустимо() -> None:
    assert picked_user({"user_id": 5, "first_name": None}) == UserInfo(5, "", None, None)


@pytest.mark.parametrize("result", [None, {}, {"user_id": None}, "x", 5, {"first_name": "Б"}])
def test_picked_user_не_выбор_даёт_none(result: object) -> None:
    assert picked_user(result) is None
