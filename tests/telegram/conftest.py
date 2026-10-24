"""Фикстуры Telegram-тестов.

Роутеры хендлеров и диалогов в приложении — модульные синглтоны: aiogram разрешает
подключить роутер к родителю только один раз, поэтому второй `build_router()` в том же
процессе падает с «Router is already attached». В бою корень строится один раз, а тестам
нужен свежий диспетчер на каждый тест, так что перед сборкой `Harness` отвязываем роутеры.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Callable

import pytest
from aiogram import Router

from meetup_bot.dialogs import MODULES as DIALOG_MODULES
from tests.helpers import App
from tests.tg_fake import Harness

_HANDLER_MODULES = (
    "meetup_bot.handlers.attendance",
    "meetup_bot.handlers.errors",
    "meetup_bot.handlers.inline",
    "meetup_bot.handlers.signup",
    "meetup_bot.handlers.start",
)


def _detach_module_routers() -> None:
    for name in (*_HANDLER_MODULES, *DIALOG_MODULES):
        module = importlib.import_module(name)
        found: list[Router] = [
            value for value in vars(module).values() if isinstance(value, Router)
        ]
        found.extend(getattr(module, "DIALOGS", []))
        for router in found:
            router._parent_router = None


@pytest.fixture
async def make_harness(app: App) -> AsyncIterator[Callable[..., Harness]]:
    """Фабрика диспетчеров: `make_harness(env="prod")`; все созданные закрываются сами."""
    created: list[Harness] = []

    def factory(*, env: str = "dev") -> Harness:
        _detach_module_routers()
        harness = Harness(app, env=env)
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        await harness.close()


@pytest.fixture
def tg(make_harness: Callable[..., Harness]) -> Harness:
    """Диспетчер в режиме dev (как в `tests/conftest.py`, но переживает повторную сборку)."""
    return make_harness()
