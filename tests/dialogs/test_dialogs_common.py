"""Общие помощники диалогов: тексты ошибок, проверка прав, определение нажавшего."""

from types import SimpleNamespace
from typing import Any

import pytest

from meetup_bot.dialogs._common import (
    ERROR_TEXTS,
    GENERIC_ERROR,
    NO_ACCESS,
    actor_id,
    describe_error,
    get_services,
    get_sessions,
    is_admin,
    middleware_value,
)
from meetup_bot.services.attendance import AlreadyMarked
from meetup_bot.services.errors import DomainError, InvalidOperation, NotFound, PermissionDenied
from meetup_bot.services.settings_schema import SettingsValidationError
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


def test_describe_error_нет_прав() -> None:
    assert describe_error(PermissionDenied("x")) == NO_ACCESS == "Нет доступа."


def test_describe_error_не_найдено() -> None:
    assert describe_error(NotFound("встреча 5")) == "Не найдено."


def test_describe_error_явка_уже_отмечена_называет_админа() -> None:
    assert describe_error(AlreadyMarked(1, "Анна")) == "Явку уже отметил(а) Анна."


def test_describe_error_явка_уже_отмечена_без_имени() -> None:
    assert describe_error(AlreadyMarked(None, None)) == "Явку уже отметил(а) другой админ."


def test_describe_error_ошибка_настройки_показывает_сообщение_валидатора() -> None:
    error = SettingsValidationError("capacity", "Нужно целое число.")
    assert describe_error(error) == "Нужно целое число."


@pytest.mark.parametrize("code", sorted(ERROR_TEXTS))
def test_describe_error_каждый_код_invalid_operation_имеет_свой_текст(code: str) -> None:
    text = describe_error(InvalidOperation(code))
    assert text == ERROR_TEXTS[code]
    assert text != GENERIC_ERROR


def test_describe_error_неизвестный_код_даёт_общий_текст() -> None:
    assert describe_error(InvalidOperation("нет_такого_кода")) == GENERIC_ERROR


def test_describe_error_произвольная_доменная_ошибка_показывает_своё_сообщение() -> None:
    assert describe_error(DomainError("особый случай")) == "особый случай"


def test_describe_error_пустая_доменная_ошибка_даёт_общий_текст() -> None:
    assert describe_error(DomainError()) == GENERIC_ERROR


def test_describe_error_непредвиденное_исключение_не_раскрывает_детали() -> None:
    text = describe_error(RuntimeError("секретная трассировка"))
    assert text == GENERIC_ERROR
    assert "секретная" not in text


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


def test_get_services_и_get_sessions_читают_middleware_data(app: App) -> None:
    manager = _manager(app, OWNER_ID)
    assert get_services(manager) is app.services
    assert get_sessions(manager) is app.sessions
    assert middleware_value(manager, "clock") is app.clock
