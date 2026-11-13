"""Один экземпляр (SPEC 2.1): команды, что ходят на сайт или меняют файлы, ждут остановки бота."""

from __future__ import annotations

import sys
from collections.abc import Callable

from rcbot.cli.exit_codes import EXIT_RUNNING
from rcbot.config.paths import Paths
from rcbot.infrastructure.win.single_instance import AlreadyRunning, SingleInstance

RUNNING_HINT = (
    "Бот запущен. Остановите работающий экземпляр (задание Планировщика или Ctrl+C) и повторите."
)


def run_exclusive(paths: Paths, body: Callable[[], int]) -> int:
    """`body` под замком бота; бот уже работает — отказ с кодом `EXIT_RUNNING`, `body` не вызван."""
    lock = SingleInstance(paths.lock_file)
    try:
        lock.acquire()
    except AlreadyRunning as error:
        print(f"{error}. {RUNNING_HINT}", file=sys.stderr)
        return EXIT_RUNNING
    try:
        return body()
    finally:
        lock.release()
