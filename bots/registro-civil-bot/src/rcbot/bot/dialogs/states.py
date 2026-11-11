"""Состояния меню (SPEC 2.5): статус, сканирование, уведомления, обслуживание."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class MenuSG(StatesGroup):
    main = State()
    status = State()
    scan = State()
    notify = State()
    maintenance = State()
