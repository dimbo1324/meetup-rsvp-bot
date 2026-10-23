"""Данные кнопок списка явки: `a:<event_id>:<registration_id>` и `a:<event_id>:d` («Готово»)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

TARGET_DONE = "d"


class AttendanceCallback(CallbackData, prefix="a", sep=":"):
    event_id: int
    target: str
