"""Находки вне зоны хранения, найденные при проверке слотов инцидента."""

from __future__ import annotations

from datetime import date, time

from rcbot.domain.models import Slot, fingerprint_of


def test_отпечаток_набора_с_днём_без_времени_и_тем_же_днём_со_временем() -> None:
    mixed = frozenset({Slot(date(2027, 2, 23)), Slot(date(2027, 2, 23), time(10, 0))})
    assert len(fingerprint_of(mixed)) == 16
