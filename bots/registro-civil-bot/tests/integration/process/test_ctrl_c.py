"""Остановка настоящего процесса по Ctrl+C доходит до `finally` и закрывает всё (SPEC 3.4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from tests.conftest import BOT_DIR


def test_sigint_в_работающем_процессе_закрывает_ресурсы_по_порядку() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "tests.integration.process.ctrlc_child"],
        cwd=BOT_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=90,
        check=False,
    )
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, done.stdout + done.stderr
    result = json.loads(lines[-1].removeprefix("RESULT "))
    order: list[str] = result["order"]

    assert done.returncode == 0, done.stderr
    assert result["interrupted"] is True  # asyncio.run перевыбросил KeyboardInterrupt после очистки
    expected = [
        "journal:bot.start",
        "msg:⏹ rcbot остановлен.",
        "telegram.cancelled",
        "bus.drain",
        "alerts.close",
        "source.close",
        "notifier.close:ntfy",
        "notifier.close:telegram",
        "bot.session.close",
        "storage.close",
        "journal:bot.stop",
    ]
    it = iter(order)
    assert all(item in it for item in expected), order
