"""Все пути бота — от его каталога (решение R2): данные, логи и кеш лежат рядом с кодом."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# src/rcbot/config/paths.py → каталог бота на три уровня выше пакета
_DEFAULT_HOME = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def default(cls) -> Paths:
        override = os.environ.get("RCBOT_HOME")
        return cls(Path(override).resolve() if override else _DEFAULT_HOME)

    @property
    def env_file(self) -> Path:
        return self.home / ".env"

    @property
    def alembic_ini(self) -> Path:
        return self.home / "alembic.ini"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def database(self) -> Path:
        return self.data / "rcbot.sqlite3"

    @property
    def lock_file(self) -> Path:
        return self.data / "rcbot.lock"

    # Каталоги прежнего пути bpmgob (до R13): бот в них больше не пишет, очистка добирает
    # оставшиеся дампы по сроку, `purge` удаляет их целиком
    @property
    def discovery(self) -> Path:
        return self.data / "discovery"

    @property
    def quarantine(self) -> Path:
        return self.data / "quarantine"

    @property
    def shots(self) -> Path:
        return self.data / "shots"

    @property
    def tmp(self) -> Path:
        return self.data / "tmp"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs / "rcbot.log"

    # Chromium прежнего пути bpmgob (до R13): больше не нужен, `purge` его удаляет
    @property
    def browsers(self) -> Path:
        return self.home / ".browsers"

    @property
    def cache(self) -> Path:
        return self.home / ".cache"

    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.database.as_posix()}"

    def ensure(self) -> None:
        for directory in (
            self.data,
            self.shots,
            self.tmp,
            self.logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def apply_process_env(self) -> None:
        """Python держит кеш байт-кода в каталоге бота (SPEC 2.7)."""
        os.environ.setdefault("PYTHONPYCACHEPREFIX", str(self.cache / "pycache"))
