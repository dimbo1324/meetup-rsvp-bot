"""Находки вне зоны хранения, найденные при проверке слотов инцидента."""

from __future__ import annotations

from datetime import date, time

import pytest

from rcbot.domain.models import Slot, fingerprint_of


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T6-2: fingerprint_of (domain/models.py) сортирует Slot; Slot(order=True) не "
    "сравнивает None со временем, и набор с днём без времени и тем же днём со временем "
    "падает с TypeError",
)
def test_отпечаток_набора_с_днём_без_времени_и_тем_же_днём_со_временем() -> None:
    mixed = frozenset({Slot(date(2027, 2, 23)), Slot(date(2027, 2, 23), time(10, 0))})
    assert len(fingerprint_of(mixed)) == 16
