"""Вход в bpmgob через ID Uruguay (SPEC 1.3): «Usuario Gub.uy» → «No tengo documento uruguayo» →
страна, тип и номер документа → пароль → возврат в bpmgob. Результат — cookies сессии в памяти.

Локаторы ищут элементы по видимому тексту и подписям, а не по CSS-классам: так они переживают
мелкие правки вёрстки. На живом сайте шаги не проверены — проверка на этапе 3 (`rcbot login
--check`). Значения секретов не логируются и не попадают в текст исключений.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from rcbot.application.ports import SessionCookie
from rcbot.config.secrets import Credentials
from rcbot.domain.errors import LoginFailed, RateLimited, SiteUnavailable
from rcbot.infrastructure.gubuy.browser import BrowserOptions, session_cookies, temporary_context
from rcbot.infrastructure.gubuy.pages import (
    BPMGOB_HOST,
    PageKind,
    classify,
    is_bpmgob_session_url,
    is_login_url,
)

logger = logging.getLogger(__name__)

STEP_TIMEOUT_MS = 30_000
RETURN_TIMEOUT_MS = 60_000


async def _first_visible(*candidates: Locator) -> Locator:
    for candidate in candidates:
        if await candidate.count() > 0:
            return candidate.first
    raise LoginFailed("на странице входа не найден ожидаемый элемент")


async def _choose(page: Page, label: re.Pattern[str], value: str) -> None:
    """Выпадающий список: обычный <select> или раскрывающийся список с вариантами."""
    field = await _first_visible(page.get_by_label(label), page.get_by_role("combobox", name=label))
    tag = await field.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        await field.select_option(label=value)
        return
    await field.click()
    await (
        await _first_visible(
            page.get_by_role("option", name=value), page.get_by_text(value, exact=True)
        )
    ).click()


async def _click_text(page: Page, pattern: re.Pattern[str]) -> None:
    target = await _first_visible(
        page.get_by_role("button", name=pattern),
        page.get_by_role("link", name=pattern),
        page.get_by_text(pattern),
    )
    await target.click()


class PlaywrightAuthenticator:
    def __init__(
        self,
        login_url: str,
        doc_country: str,
        doc_type: str,
        credentials: Callable[[], Credentials],
        options: BrowserOptions,
    ) -> None:
        self._login_url = login_url
        self._doc_country = doc_country
        self._doc_type = doc_type
        self._credentials = credentials
        self._options = options
        self._step = ""

    async def login(self) -> list[SessionCookie]:
        creds = self._credentials()
        self._step = "открытие страницы входа"
        try:
            async with temporary_context(self._options) as context:
                page = await context.new_page()
                response = await page.goto(self._login_url, wait_until="domcontentloaded")
                self._check_blocked(
                    page.url, await page.content(), response.status if response else None
                )
                await self._fill_identity(page, creds)
                self._step = "возврат на bpmgob"
                await page.wait_for_url(is_bpmgob_session_url, timeout=RETURN_TIMEOUT_MS)
                cookies = await session_cookies(context, [BPMGOB_HOST])
        except PlaywrightError as error:
            # Текст ошибки Playwright может содержать адрес и селектор, но не введённые значения
            # Название шага — единственное, что видно в журнале при сбое: на живом сайте именно
            # по нему станет ясно, где вход застрял
            raise SiteUnavailable(
                f"вход не завершился на шаге «{self._step}»: {type(error).__name__}"
            ) from None
        if not cookies:
            raise LoginFailed("после входа нет cookies сессии bpmgob")
        logger.info("вход выполнен: получено %d cookies сессии", len(cookies))
        return cookies

    @staticmethod
    def _check_blocked(url: str, text: str, status: int | None) -> None:
        if classify(url, text, status) is PageKind.BLOCKED:
            raise RateLimited("страница входа заблокирована защитным шлюзом")

    async def _fill_identity(self, page: Page, creds: Credentials) -> None:
        self._step = "переход на ID Uruguay"
        if not is_login_url(page.url):
            await page.wait_for_url(is_login_url, timeout=STEP_TIMEOUT_MS)
        self._step = "выбор «Usuario Gub.uy»"
        await _click_text(page, re.compile(r"Usuario\s+Gub\.uy", re.I))
        self._step = "«No tengo documento uruguayo»"
        await _click_text(page, re.compile(r"No tengo documento uruguayo", re.I))
        self._step = "страна и тип документа"
        await _choose(page, re.compile(r"Pa[ií]s emisor", re.I), self._doc_country)
        await _choose(page, re.compile(r"Tipo de documento", re.I), self._doc_type)
        self._step = "номер документа"
        number = await _first_visible(
            page.get_by_label(re.compile(r"N[úu]mero", re.I)),
            page.get_by_role("textbox", name=re.compile(r"N[úu]mero", re.I)),
        )
        await number.fill(creds.doc_number.get_secret_value())
        await _click_text(page, re.compile(r"^\s*Continuar\s*$", re.I))

        self._step = "пароль"
        password = await _first_visible(
            page.get_by_label(re.compile(r"contrase[ñn]a", re.I)),
            page.locator("input[type=password]"),
        )
        await password.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
        await password.fill(creds.password.get_secret_value())
        await _click_text(page, re.compile(r"^\s*Continuar\s*$", re.I))
