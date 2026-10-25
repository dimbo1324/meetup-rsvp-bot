"""Календарь aiogram-dialog с русскими подписями.

Стандартный календарь берёт названия месяцев и дней недели из локали ОС (`%B`, `%a`): на
сервере это английский. Здесь подписи строятся из `texts/render`, а не из локали.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from aiogram_dialog import DialogManager
from aiogram_dialog.widgets.kbd import Calendar
from aiogram_dialog.widgets.kbd.calendar_kbd import (
    CalendarDaysView,
    CalendarMonthView,
    CalendarScope,
    CalendarScopeView,
    CalendarUserConfig,
    CalendarYearsView,
)
from aiogram_dialog.widgets.text import Text

from meetup_bot.texts.render import MONTHS_NOMINATIVE, WEEKDAYS_SHORT

HEADER_ICON = "🗓"
PREV_MARK = "<<"
NEXT_MARK = ">>"

UserConfigGetter = Callable[[DialogManager], Awaitable[CalendarUserConfig]]


def month_name(day: date) -> str:
    return MONTHS_NOMINATIVE[day.month - 1].capitalize()


def month_year(day: date) -> str:
    return f"{month_name(day)} {day.year}"


class _DateText(Text):
    """Подпись кнопки из даты, которую календарь кладёт в данные под ключом `date`."""

    def __init__(self, render: Callable[[date], str]) -> None:
        super().__init__()
        self._render = render

    async def _render_text(self, data: dict[str, Any], manager: DialogManager) -> str:
        day: date = data["date"]
        return self._render(day)


class RussianCalendar(Calendar):
    """Календарь с русскими подписями; `user_config` задаёт, например, часовой пояс «сегодня»."""

    def __init__(
        self,
        id: str,
        on_click: Any = None,
        user_config: UserConfigGetter | None = None,
    ) -> None:
        super().__init__(id=id, on_click=on_click)
        self._user_config = user_config

    def _init_views(self) -> dict[CalendarScope, CalendarScopeView]:
        callback = self._item_callback_data
        return {
            CalendarScope.DAYS: CalendarDaysView(
                callback,
                weekday_text=_DateText(lambda day: WEEKDAYS_SHORT[day.weekday()]),
                header_text=_DateText(lambda day: f"{HEADER_ICON} {month_year(day)}"),
                prev_month_text=_DateText(lambda day: f"{PREV_MARK} {month_year(day)}"),
                next_month_text=_DateText(lambda day: f"{month_year(day)} {NEXT_MARK}"),
            ),
            CalendarScope.MONTHS: CalendarMonthView(
                callback,
                month_text=_DateText(month_name),
                this_month_text=_DateText(lambda day: f"[ {month_name(day)} ]"),
            ),
            CalendarScope.YEARS: CalendarYearsView(callback),
        }

    async def _get_user_config(
        self, data: dict[str, Any], manager: DialogManager
    ) -> CalendarUserConfig:
        if self._user_config is None:
            return CalendarUserConfig()
        return await self._user_config(manager)
