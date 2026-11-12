"""Редактор вызывается синхронно из каждой записи журнала и лога: он обязан работать быстро."""

from __future__ import annotations

import time

from rcbot.infrastructure.logging.redaction import REDACTOR


def test_длинная_строка_без_пробелов_редактируется_быстро() -> None:
    started = time.perf_counter()
    REDACTOR.redact("a" * 20_000)
    assert time.perf_counter() - started < 0.5
