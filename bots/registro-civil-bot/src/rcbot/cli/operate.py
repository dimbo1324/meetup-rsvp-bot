"""Рабочие команды: discover, login, check, run, simulate. Наполняются в фазах P2–P6 этапа 1."""

from __future__ import annotations

import argparse
import sys


def _not_yet(name: str) -> int:
    print(f"Команда `rcbot {name}` ещё не реализована (этап 1 в работе).", file=sys.stderr)
    return 1


def cmd_discover(_: argparse.Namespace) -> int:
    return _not_yet("discover")


def cmd_login(_: argparse.Namespace) -> int:
    return _not_yet("login")


def cmd_check(_: argparse.Namespace) -> int:
    return _not_yet("check")


def cmd_run(_: argparse.Namespace) -> int:
    return _not_yet("run")


def cmd_simulate(_: argparse.Namespace) -> int:
    return _not_yet("simulate")
