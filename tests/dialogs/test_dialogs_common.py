"""Общие помощники диалогов: проверка прав, определение нажавшего, данные middleware."""

from types import SimpleNamespace
from typing import Any

from meetup_bot.dialogs._common import (
    actor_id,
    get_services,
    get_sessions,
    is_admin,
    middleware_value,
)
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


def test_get_services_и_get_sessions_читают_middleware_data(app: App) -> None:
    manager = _manager(app, OWNER_ID)
    assert get_services(manager) is app.services
    assert get_sessions(manager) is app.sessions
    assert middleware_value(manager, "clock") is app.clock
