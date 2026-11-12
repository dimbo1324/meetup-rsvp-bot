"""Общие фикстуры тестов rcbot (решение R7).

Автоматически для КАЖДОГО теста (тесты не должны ни читать, ни портить настоящее окружение):
- RCBOT_* из окружения удалены; RCBOT_HOME — временный «каталог бота» с копией alembic.ini;
  журнал наблюдения — во временной папке, а не на рабочем столе;
- keyring — в памяти: Windows Credential Manager тесты не видят и не меняют;
- фильтр секретов (REDACTOR) и корневой логгер возвращаются в исходное состояние.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import keyring
import keyring.backend
import keyring.errors
import pytest
from hypothesis import HealthCheck, settings

import rcbot
from rcbot.config.paths import Paths
from rcbot.infrastructure.logging.redaction import REDACTOR
from tests.fakes import FakeClock, LocalTlsServer, ManualClock

BOT_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS = Path(rcbot.__file__).resolve().parent / "infrastructure" / "db" / "migrations"

# Браузер лежит в каталоге бота; worktree и CI могут задать свой путь заранее
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(BOT_DIR / ".browsers"))

settings.register_profile(
    "rcbot",
    deadline=None,
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.load_profile("rcbot")


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError("нет такого секрета")
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def bot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Временный каталог бота: RCBOT_HOME указывает на него, миграции находят свой скрипт."""
    for name in [n for n in os.environ if n.startswith("RCBOT_")]:
        monkeypatch.delenv(name)
    home = tmp_path / "bot-home"
    home.mkdir()
    (home / "alembic.ini").write_text(
        f"[alembic]\nscript_location = {MIGRATIONS.as_posix()}\n"
        f"prepend_sys_path = {MIGRATIONS.parents[3].as_posix()}\npath_separator = os\n",
        encoding="ascii",
    )
    monkeypatch.setenv("RCBOT_HOME", str(home))
    monkeypatch.setenv("RCBOT_JOURNAL_DIR", str(tmp_path / "journal"))
    # В тестах журнал пишется синхронно: файлы читаются сразу. Фоновый режим проверяют
    # отдельные тесты (tests/unit/storage/test_journal_background.py, e2e)
    monkeypatch.setenv("RCBOT_JOURNAL_BACKGROUND", "false")
    return home


@pytest.fixture
def paths(bot_home: Path) -> Paths:
    return Paths(bot_home)


@pytest.fixture(autouse=True)
def clean_redactor() -> Iterator[None]:
    REDACTOR.clear()
    yield
    REDACTOR.clear()


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def manual_clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
async def tls_server() -> AsyncIterator[LocalTlsServer]:
    async with LocalTlsServer() as server:
        yield server


_CHROMIUM: bool | None = None


def chromium_ready() -> bool:
    global _CHROMIUM
    if _CHROMIUM is None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                _CHROMIUM = Path(pw.chromium.executable_path).exists()
        except Exception:  # Playwright сломан или не установлен — тесты браузера пропускаются
            _CHROMIUM = False
    return _CHROMIUM


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("browser") and not chromium_ready():
        pytest.skip("Chromium не установлен: uv run python dev.py setup --bot registro-civil-bot")
