"""Редактор вызывается синхронно из каждой записи журнала и лога: он обязан работать быстро."""

from __future__ import annotations

import time

import pytest

from rcbot.infrastructure.logging.redaction import REDACTOR


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T6-1: шаблон почты `[\\w.+-]+@` квадратичен на длинной строке без пробелов "
    "(20 КБ ≈ 2,5 с, 100 КБ ≈ минута): тело ответа в ошибке подвешивает цикл событий",
)
def test_длинная_строка_без_пробелов_редактируется_быстро() -> None:
    started = time.perf_counter()
    REDACTOR.redact("a" * 20_000)
    assert time.perf_counter() - started < 0.5
