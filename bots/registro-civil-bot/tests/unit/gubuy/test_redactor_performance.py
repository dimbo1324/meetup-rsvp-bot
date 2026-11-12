"""Находка вне зоны T4: редактор секретов на длинной строке без разделителей (зона T8)."""

from __future__ import annotations

import time

from rcbot.infrastructure.logging.redaction import REDACTOR


def test_редактор_не_квадратичен_на_длинной_строке_без_разделителей() -> None:
    text = "a" * 20_000
    started = time.perf_counter()
    REDACTOR.redact(text)
    assert time.perf_counter() - started < 0.5
