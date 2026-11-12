"""Запуск Chromium для входа и запасного режима: без профиля на диске (SPEC 2.3, 2.6).

`new_context()` даёт временный контекст в памяти: cookies и кеш исчезают вместе с ним.
Отпечаток браузера не подменяется (SPEC 0.3, правило 2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from rcbot.application.ports import SessionCookie
from rcbot.infrastructure.logging.redaction import REDACTOR

NAVIGATION_TIMEOUT_MS = 45_000


@dataclass(frozen=True)
class BrowserOptions:
    headless: bool = True
    channel: str = ""


async def launch(pw: Playwright, options: BrowserOptions) -> Browser:
    kwargs: dict[str, object] = {"headless": options.headless}
    if options.channel:
        kwargs["channel"] = options.channel
    return await pw.chromium.launch(**kwargs)  # type: ignore[arg-type]


@asynccontextmanager
async def temporary_context(options: BrowserOptions) -> AsyncIterator[BrowserContext]:
    async with async_playwright() as pw:
        browser = await launch(pw, options)
        try:
            context = await browser.new_context()
            context.set_default_timeout(NAVIGATION_TIMEOUT_MS)
            yield context
        finally:
            await browser.close()


def to_playwright(cookies: Sequence[SessionCookie]) -> list[dict[str, object]]:
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure}
        for c in cookies
    ]


async def session_cookies(context: BrowserContext, hosts: Sequence[str]) -> list[SessionCookie]:
    cookies = await context.cookies([f"https://{host}/" for host in hosts])
    # Значение cookie регистрируется в фильтре: «голое» эхо в ответе, исключении или JSON
    # (шаблоны знают только имена simple_bpm_*) иначе попало бы в лог, карантин и журнал
    REDACTOR.register_many(str(c.get("value", "")) for c in cookies if c.get("name"))
    return [
        SessionCookie(
            name=str(c.get("name", "")),
            value=str(c.get("value", "")),
            domain=str(c.get("domain", "")),
            path=str(c.get("path", "/")),
            secure=bool(c.get("secure", True)),
        )
        for c in cookies
        if c.get("name")
    ]
