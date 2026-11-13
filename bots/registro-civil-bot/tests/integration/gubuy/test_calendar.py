"""Общий модуль чтения календаря в Chromium: строгий и терпимый режимы (SPEC 0.3, 2.3)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright
from tests.integration.gubuy.site import OFFICE, MonthSpec, agenda_page, green, grey, rows

from rcbot.domain.errors import UnexpectedResponse
from rcbot.infrastructure.gubuy.calendar import next_month, select_office

pytestmark = pytest.mark.browser

MONTHS = [
    MonthSpec("Noviembre 2026", rows(grey(1), green(5))),
    MonthSpec("Diciembre 2026", rows(green(3))),
]


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def test_офис_выбирается_в_обоих_режимах(page: Page) -> None:
    for strict in (True, False):
        await page.set_content(agenda_page(MONTHS))
        await select_office(page, OFFICE, strict=strict)
        assert await page.locator("#loc1").is_checked()


async def test_нет_переключателя_строгий_режим_ошибка_терпимый_молчит(page: Page) -> None:
    await page.set_content(agenda_page(MONTHS, radio=False))
    with pytest.raises(UnexpectedResponse, match="переключатель офиса"):
        await select_office(page, OFFICE, strict=True)
    await select_office(page, OFFICE, strict=False)


@pytest.mark.parametrize("strict", [True, False])
async def test_месяц_листается_в_обоих_режимах(page: Page, strict: bool) -> None:
    await page.set_content(agenda_page(MONTHS))
    await select_office(page, OFFICE, strict=True)
    await next_month(page, "Noviembre 2026", strict=strict)
    assert await page.locator(".ui-datepicker-title").text_content() == "Diciembre 2026"


async def test_месяц_не_сменился_строгий_режим_ошибка(page: Page) -> None:
    await page.set_content(agenda_page(MONTHS, flip="none"))
    await select_office(page, OFFICE, strict=True)
    page.set_default_timeout(500)
    with pytest.raises(UnexpectedResponse, match="не перелистнулся"):
        await next_month(page, "Noviembre 2026", strict=True)
