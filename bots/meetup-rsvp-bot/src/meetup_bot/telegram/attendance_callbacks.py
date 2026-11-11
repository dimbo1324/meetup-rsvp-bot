"""Данные кнопок списка явки: `a:<event_id>:<цель>`.

Цель: id записи (переключить отметку), `d` («Готово»), `p<номер>` (страница), `i` (номер
страницы — кнопка-надпись, нажатие ничего не делает).
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

TARGET_DONE = "d"
TARGET_NOOP = "i"
PAGE_PREFIX = "p"


class AttendanceCallback(CallbackData, prefix="a", sep=":"):
    event_id: int
    target: str
