"""Дочерний процесс теста Ctrl+C: настоящий `asyncio.run` и настоящий SIGINT самому себе.

Запуск только из test_ctrl_c.py: `python -m tests.integration.process.ctrlc_child`.
"""

from __future__ import annotations

import asyncio
import json

from tests.integration.process.harness import build_rig, install_memory_keyring

from rcbot import runner


def main() -> None:
    install_memory_keyring()
    rig = build_rig(lambda obj, name, value: setattr(obj, name, value))
    rig.sigint_on_started = True  # SIGINT придёт через 0,1 с после сообщения о запуске
    interrupted = False
    try:
        asyncio.run(runner.run_app(rig.core))
    except KeyboardInterrupt:
        interrupted = True
    print("RESULT", json.dumps({"interrupted": interrupted, "order": rig.order}), flush=True)


main()
