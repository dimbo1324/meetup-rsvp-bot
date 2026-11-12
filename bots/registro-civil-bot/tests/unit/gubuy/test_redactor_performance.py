"""Находка вне зоны T4: редактор секретов на длинной строке без разделителей (зона T8)."""

from __future__ import annotations

import time

import pytest

from rcbot.infrastructure.logging.redaction import REDACTOR


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T4: шаблон почты `[\\w.+-]+@…` в logging/redaction.py квадратичен: 20 000 знаков "
    "без разделителей — ~2 с, 40 000 — ~20 с. Карантин и разведка прогоняют через редактор "
    "тела ответов прямо в цикле опроса; ответ с длинным base64/hex застопорит опрос",
)
def test_редактор_не_квадратичен_на_длинной_строке_без_разделителей() -> None:
    text = "a" * 20_000
    started = time.perf_counter()
    REDACTOR.redact(text)
    assert time.perf_counter() - started < 0.5
