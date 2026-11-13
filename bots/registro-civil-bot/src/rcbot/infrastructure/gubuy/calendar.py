"""Общие действия чтения календаря (SPEC 0.3): выбор офиса и перелистывание месяца.

Единственное место, где код календаря трогает страницу: радиокнопка офиса (`check`) и стрелка
следующего месяца (`click`). Дни, «Siguiente», «Guardar y Cerrar» и «Volver» не трогаются никогда.
`strict=True` — для `DomSource`: отсутствие элемента или неперелистнувшийся месяц — ошибка
`UnexpectedResponse`; `strict=False` — для разведки: на живой странице она идёт за владельцем и
не должна падать на мелочи.
"""

from __future__ import annotations

import re

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from rcbot.domain.errors import UnexpectedResponse

ROOT = ".ui-datepicker"
CALENDAR = ".ui-datepicker-calendar"
NEXT_MONTH = ".ui-datepicker-next"


async def select_office(page: Page, location_text: str, *, strict: bool) -> None:
    """Выбирает радиокнопку офиса; без неё календарь на шаге «Agenda» не появляется."""
    pattern = re.compile(re.escape(location_text), re.I)
    radio = page.get_by_role("radio", name=pattern)
    if strict and await radio.count() == 0:
        radio = page.get_by_label(pattern)
    if await radio.count() == 0:
        if strict:
            raise UnexpectedResponse("переключатель офиса на шаге «Agenda» не найден")
        return
    if not await radio.first.is_checked():
        await radio.first.check()


async def next_month(page: Page, current_title: str, *, strict: bool) -> None:
    """Нажимает стрелку следующего месяца и ждёт смены месяца."""
    await page.locator(NEXT_MONTH).first.click()
    if not strict:
        await page.wait_for_load_state("networkidle")
        return
    try:
        await page.wait_for_function(
            "t => (document.querySelector('.ui-datepicker-title') || {}).textContent !== t",
            arg=current_title,
        )
    except PlaywrightTimeout:
        # Стрелка была, нажатие её отработало, а месяц не сменился: страница устроена не так
        raise UnexpectedResponse("календарь не перелистнулся на следующий месяц") from None
