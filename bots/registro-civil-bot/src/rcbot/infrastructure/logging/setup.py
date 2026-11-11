"""Логи: файл с ротацией 5 × 2 МБ (SPEC 2.7) и консоль, если она есть; фильтр вырезания на всём."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rcbot.infrastructure.logging.redaction import REDACTOR, RedactionFilter

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 5
# Внешние библиотеки многословны и могут печатать заголовки: только предупреждения
_QUIET = ("aiohttp", "aiogram", "aiogram_dialog", "sqlalchemy", "alembic", "asyncio", "playwright")


def setup_logging(log_file: Path, level: str = "INFO", console: bool = True) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(level)

    redaction = RedactionFilter(REDACTOR)
    formatter = logging.Formatter(FORMAT)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    ]
    # pythonw.exe запускается без консоли: sys.stderr там None
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(redaction)
        root.addHandler(handler)
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
