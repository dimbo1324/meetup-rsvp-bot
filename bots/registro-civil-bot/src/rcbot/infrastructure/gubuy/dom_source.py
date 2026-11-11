"""`DomSource` — запасной источник (SPEC 2.3, D1): календарь читается в Chromium без окна.

Одна вкладка на шаге «Agenda». За проверку: перезагрузка шага, выбор офиса (без него календарь не
появляется), чтение зелёных дней текущего месяца и переход стрелкой к следующему. Дни не
нажимаются: нажатие выбрало бы дату (SPEC 0.3, правило 1), поэтому время внутри дня неизвестно.
Кнопки «Siguiente», «Guardar y Cerrar», «Volver» не трогаются никогда.
"""

from __future__ import annotations

import logging
import re
import time as monotonic_time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from rcbot.application.ports import Clock, SessionCookie
from rcbot.domain.errors import (
    ManualStepRequired,
    RateLimited,
    SessionExpired,
    SiteUnavailable,
    UnexpectedResponse,
)
from rcbot.domain.models import AvailabilitySnapshot, Slot, SourceKind, WatchTarget
from rcbot.infrastructure.gubuy.browser import BrowserOptions, launch, to_playwright
from rcbot.infrastructure.gubuy.pages import PageKind, classify
from rcbot.infrastructure.gubuy.parsers import (
    READ_MONTH_JS,
    month_view,
    parse_month_title,
    slots_of,
)

logger = logging.getLogger(__name__)

CALENDAR = ".ui-datepicker-calendar"
NEXT_MONTH = ".ui-datepicker-next"
STEP_TIMEOUT_MS = 20_000
MAX_SHOTS = 20


class DomSource:
    def __init__(
        self,
        agenda_url: str,
        location_text: str,
        options: BrowserOptions,
        shots_dir: Path,
        clock: Clock,
    ) -> None:
        self._agenda_url = agenda_url
        self._location = location_text
        self._options = options
        self._shots_dir = shots_dir
        self._clock = clock
        self._cookies: list[SessionCookie] = []
        self._cookies_dirty = False
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def kind(self) -> SourceKind:
        return SourceKind.DOM

    def use_cookies(self, cookies: Sequence[SessionCookie]) -> None:
        self._cookies = list(cookies)
        self._cookies_dirty = True

    async def _ensure_page(self) -> Page:
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            self._browser = await launch(self._pw, self._options)
            self._context = None
        if self._context is None or self._cookies_dirty:
            if self._context is not None:
                await self._context.close()
            # Временный контекст: профиль и cookies не пишутся на диск
            self._context = await self._browser.new_context()
            self._context.set_default_timeout(STEP_TIMEOUT_MS)
            if self._cookies:
                await self._context.add_cookies(to_playwright(self._cookies))  # type: ignore[arg-type]
            self._cookies_dirty = False
            self._page = None
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        started = monotonic_time.perf_counter()
        try:
            page = await self._ensure_page()
            response = await page.goto(self._agenda_url, wait_until="domcontentloaded")
            self._raise_for_page(
                page.url, await page.content(), response.status if response else None
            )
            await self._select_location(page)
            await page.wait_for_selector(CALENDAR, state="visible")
            slots = await self._read_months(page, target.months_ahead)
        except PlaywrightError as error:
            raise SiteUnavailable(f"браузер не прочитал страницу: {type(error).__name__}") from None
        latency_ms = (monotonic_time.perf_counter() - started) * 1000
        snapshot = AvailabilitySnapshot(
            target.id, self._clock.now(), slots, SourceKind.DOM, latency_ms
        )
        if slots:
            await self._shot(page, snapshot.observed_at)
        return snapshot

    @staticmethod
    def _raise_for_page(url: str, text: str, status: int | None) -> None:
        kind = classify(url, text, status)
        if kind is PageKind.LOGIN:
            raise SessionExpired("сайт открыл страницу входа")
        if kind is PageKind.STEP1:
            raise ManualStepRequired("сайт требует пройти шаг 1 с контрольным вопросом")
        if kind is PageKind.BLOCKED:
            raise RateLimited("страница блокировки защитного шлюза")
        if status is not None and status >= 500:
            raise SiteUnavailable(f"сайт ответил {status}")
        if kind is PageKind.UNKNOWN:
            raise UnexpectedResponse("признаки шага «Agenda» на странице не найдены")

    async def _select_location(self, page: Page) -> None:
        pattern = re.compile(re.escape(self._location), re.I)
        radio = page.get_by_role("radio", name=pattern)
        if await radio.count() == 0:
            radio = page.get_by_label(pattern)
        if await radio.count() == 0:
            raise UnexpectedResponse("переключатель офиса на шаге «Agenda» не найден")
        if not await radio.first.is_checked():
            await radio.first.check()

    async def _read_months(self, page: Page, months: int) -> frozenset[Slot]:
        found: set[Slot] = set()
        seen: set[tuple[int, int]] = set()
        for index in range(months):
            view = month_view(await page.evaluate(READ_MONTH_JS))
            month = parse_month_title(view.title)
            if month in seen:
                raise UnexpectedResponse("календарь не перелистнулся на следующий месяц")
            seen.add(month)
            found |= slots_of(view)
            if index == months - 1 or not view.has_next:
                break
            await page.locator(NEXT_MONTH).first.click()
            await page.wait_for_function(
                "t => (document.querySelector('.ui-datepicker-title') || {}).textContent !== t",
                arg=view.title,
            )
        return frozenset(found)

    async def _shot(self, page: Page, at: datetime) -> None:
        """Снимок только элемента календаря: личных данных страницы в нём нет (SPEC 2.6)."""
        try:
            self._shots_dir.mkdir(parents=True, exist_ok=True)
            await page.locator(".ui-datepicker").first.screenshot(
                path=str(self._shots_dir / f"{at:%Y%m%dT%H%M%S}.png")
            )
            for old in sorted(self._shots_dir.glob("*.png"))[:-MAX_SHOTS]:
                old.unlink(missing_ok=True)
        except PlaywrightError as error:
            logger.warning("снимок календаря не сохранён: %s", type(error).__name__)

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._browser = self._context = self._page = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None
