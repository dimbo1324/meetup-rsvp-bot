"""Проверка доступа к шагу «Agenda» с готовыми cookies: что показывает сайт после входа (D3)."""

from __future__ import annotations

from collections.abc import Sequence

from rcbot.application.ports import SessionCookie
from rcbot.infrastructure.gubuy.browser import BrowserOptions, temporary_context, to_playwright
from rcbot.infrastructure.gubuy.pages import PageKind, classify


async def probe_agenda(
    agenda_url: str, cookies: Sequence[SessionCookie], options: BrowserOptions
) -> PageKind:
    async with temporary_context(options) as context:
        await context.add_cookies(to_playwright(cookies))  # type: ignore[arg-type]
        page = await context.new_page()
        response = await page.goto(agenda_url, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        return classify(page.url, await page.content(), response.status if response else None)
