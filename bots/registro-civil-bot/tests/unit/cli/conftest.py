"""Общие помощники тестов CLI: окружение процесса, .env, чтение журнала наблюдения."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.conftest import BOT_DIR

from rcbot.config.paths import Paths


@pytest.fixture(autouse=True)
def restore_process_env() -> Iterator[None]:
    """`build_core` через `setdefault` пишет PYTHONPYCACHEPREFIX и PLAYWRIGHT_BROWSERS_PATH в
    окружение процесса: после теста возвращаем его, чтобы соседи не видели чужих значений."""
    before = dict(os.environ)
    yield
    for name in set(os.environ) - set(before):
        del os.environ[name]
    os.environ.update(before)


@pytest.fixture
def env_example(bot_home: Path) -> Path:
    """В `bot_home` только alembic.ini, а `rcbot init` копирует .env.example из каталога бота."""
    target = bot_home / ".env.example"
    shutil.copyfile(BOT_DIR / ".env.example", target)
    return target


def write_env(paths: Paths, **values: str) -> None:
    """`.env` бота: `write_env(paths, poll_interval_sec="5")` → RCBOT_POLL_INTERVAL_SEC=5."""
    lines = [f"RCBOT_{name.upper()}={value}" for name, value in values.items()]
    paths.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def journal_events(journal_dir: Path) -> list[dict[str, object]]:
    """Все события журнала наблюдения (по всем дням) по порядку записи."""
    events: list[dict[str, object]] = []
    for file in sorted((journal_dir / "events").glob("events-*.jsonl")):
        events += [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
    return events


def journal_checks(journal_dir: Path) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    for file in sorted((journal_dir / "checks").glob("checks-*.jsonl")):
        checks += [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()]
    return checks
