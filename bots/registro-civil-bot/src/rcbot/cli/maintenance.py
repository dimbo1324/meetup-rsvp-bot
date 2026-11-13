"""Обслуживание (SPEC 2.7, 4.7): `rcbot purge` и `rcbot uninstall`."""

from __future__ import annotations

import argparse
import subprocess
import sys

from rcbot.bootstrap import build_core
from rcbot.cli.exclusive import run_exclusive
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretKey, SecretStore
from rcbot.infrastructure.files import purge

TASK_NAME = "rcbot"


def cmd_purge(_: argparse.Namespace) -> int:
    paths = Paths.default()
    return run_exclusive(paths, lambda: _purge(paths))


def _purge(paths: Paths) -> int:
    removed = purge(paths)
    if not removed:
        print("Удалять нечего.")
    for directory in removed:
        print(f"Удалено: {directory}")
    print("База данных, .env и Chromium не тронуты.")
    return 0


def cmd_uninstall(_: argparse.Namespace) -> int:
    with build_core(console=False, command="uninstall") as core:
        return run_exclusive(core.paths, lambda: _uninstall(core.paths))


def _uninstall(paths: Paths) -> int:
    answer = input(
        "Удалить все секреты rcbot из Credential Manager и задание Планировщика? [да/нет] "
    )
    if answer.strip().lower() not in ("да", "y", "yes", "д"):
        print("Отменено.")
        return 1
    store = SecretStore()
    for key in SecretKey:
        if store.delete(key):
            print(f"Секрет удалён: {key.value}")
    task_removed = _remove_scheduler_task() if sys.platform == "win32" else True
    print(
        f"Готово. Чтобы убрать всё остальное: `rcbot purge`, затем можно удалить каталог\n"
        f"{paths.home} (база, .env и Chromium лежат в нём)."
    )
    return 0 if task_removed else 1


def _remove_scheduler_task() -> bool:
    """Нет задания — норма; задание есть, но не удалилось — об этом нужно сказать (SPEC 8.10)."""
    exists = _schtasks("/Query")
    if exists.returncode != 0:
        print("Задания Планировщика нет.")
        return True
    if _schtasks("/Delete", "/F").returncode == 0:
        print("Задание Планировщика удалено.")
        return True
    print(
        f"Задание Планировщика не удалено: выполните `schtasks /Delete /TN {TASK_NAME} /F` "
        "от имени владельца задания.",
        file=sys.stderr,
    )
    return False


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["schtasks", args[0], "/TN", TASK_NAME, *args[1:]],
        capture_output=True,
        text=True,
        check=False,
    )
