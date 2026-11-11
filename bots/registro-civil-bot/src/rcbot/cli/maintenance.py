"""Обслуживание: purge и uninstall. Наполняются в фазе P7 этапа 1."""

from __future__ import annotations

import argparse
import sys


def cmd_purge(_: argparse.Namespace) -> int:
    print("Команда `rcbot purge` ещё не реализована (этап 1 в работе).", file=sys.stderr)
    return 1


def cmd_uninstall(_: argparse.Namespace) -> int:
    print("Команда `rcbot uninstall` ещё не реализована (этап 1 в работе).", file=sys.stderr)
    return 1
