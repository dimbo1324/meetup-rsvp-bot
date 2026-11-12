"""Обслуживание (SPEC 2.7, 4.7): `rcbot purge` и `rcbot uninstall`."""

from __future__ import annotations

import argparse
import subprocess
import sys

from rcbot.bootstrap import build_core
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretKey, SecretStore
from rcbot.infrastructure.files import purge
from rcbot.infrastructure.win.single_instance import AlreadyRunning, SingleInstance

TASK_NAME = "rcbot"


def _not_running(paths: Paths) -> bool:
    lock = SingleInstance(paths.lock_file)
    try:
        lock.acquire()
    except AlreadyRunning:
        print("Бот запущен: остановите его (задание Планировщика или Ctrl+C) и повторите.")
        return False
    lock.release()
    return True


def cmd_purge(_: argparse.Namespace) -> int:
    paths = Paths.default()
    if not _not_running(paths):
        return 3
    removed = purge(paths)
    if not removed:
        print("Удалять нечего.")
    for directory in removed:
        print(f"Удалено: {directory}")
    print("База данных, .env и Chromium не тронуты.")
    return 0


def cmd_uninstall(_: argparse.Namespace) -> int:
    core = build_core(console=False, command="uninstall")
    if not _not_running(core.paths):
        return 3
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
    if sys.platform == "win32":
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        print(
            "Задание Планировщика удалено."
            if result.returncode == 0
            else "Задания Планировщика нет."
        )
    print(
        f"Готово. Чтобы убрать всё остальное: `rcbot purge`, затем можно удалить каталог\n"
        f"{core.paths.home} (база, .env и Chromium лежат в нём)."
    )
    return 0
