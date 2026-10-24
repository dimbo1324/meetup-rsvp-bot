"""Фикстуры Telegram-тестов (отвязка модульных роутеров выполняется в `tests.tg_fake.Harness`)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import pytest

from tests.helpers import App
from tests.tg_fake import Harness


@pytest.fixture
async def make_harness(app: App) -> AsyncIterator[Callable[..., Harness]]:
    """Фабрика диспетчеров: `make_harness(env="prod")`; все созданные закрываются сами."""
    created: list[Harness] = []

    def factory(*, env: str = "dev") -> Harness:
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
