"""Запуск браузера для входа и запасного режима: без профиля на диске (SPEC 2.3, 2.6).

Браузер любой на движке Chromium (решение R8): по умолчанию первым берётся установленный Google
Chrome, затем Microsoft Edge, затем встроенный Chromium Playwright. Явный выбор в настройке
`RCBOT_BROWSER` запасных вариантов не имеет: владелец сказал, чем пользоваться.

`new_context()` даёт временный контекст в памяти: cookies и кеш исчезают вместе с ним.
Отпечаток браузера не подменяется (SPEC 0.3, правило 2).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from rcbot.application.ports import SessionCookie
from rcbot.infrastructure.logging.redaction import REDACTOR

logger = logging.getLogger(__name__)

NAVIGATION_TIMEOUT_MS = 45_000

AUTO = "auto"
CHROME = "chrome"
EDGE = "msedge"
BUNDLED = "chromium"
AUTO_ORDER = (CHROME, EDGE, BUNDLED)
NAMES = {CHROME: "Google Chrome", EDGE: "Microsoft Edge", BUNDLED: "Chromium Playwright"}

# Где Playwright ищет установленные Chrome и Edge в Windows (то же ищем и мы для `doctor`)
_WINDOWS_PARTS = {
    CHROME: ("Google", "Chrome", "Application", "chrome.exe"),
    EDGE: ("Microsoft", "Edge", "Application", "msedge.exe"),
}
_WINDOWS_ROOTS = ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")


@dataclass(frozen=True)
class BrowserOptions:
    """`channel` — выбор из настройки: `auto`, `chrome`, `msedge` или `chromium`.

    По умолчанию — встроенный Chromium: так ведут себя тесты и код, не знающий про выбор. Бот
    собирает параметры из настройки (`bootstrap.browser_options`).
    """

    headless: bool = True
    channel: str = BUNDLED


def channel_ladder(choice: str) -> tuple[str, ...]:
    """Каналы по порядку попыток: `auto` — лестница, остальное — ровно один канал."""
    return AUTO_ORDER if choice == AUTO else (choice,)


def installed_path(channel: str, env: Mapping[str, str] | None = None) -> Path | None:
    """Путь к установленному Chrome или Edge (Windows); для остальных каналов — `None`."""
    parts = _WINDOWS_PARTS.get(channel)
    if parts is None:
        return None
    source = os.environ if env is None else env
    for name in _WINDOWS_ROOTS:
        if root := source.get(name):
            candidate = Path(root, *parts)
            if candidate.is_file():
                return candidate
    return None


@dataclass(frozen=True)
class BrowserSurvey:
    """Что нашлось на машине: пути по каналам и тот, что будет использован."""

    found: dict[str, Path | None]
    chosen: str | None


def survey(
    choice: str, bundled: Path | None, env: Mapping[str, str] | None = None
) -> BrowserSurvey:
    found: dict[str, Path | None] = {
        CHROME: installed_path(CHROME, env),
        EDGE: installed_path(EDGE, env),
        BUNDLED: bundled,
    }
    chosen = next((c for c in channel_ladder(choice) if found.get(c) is not None), None)
    return BrowserSurvey(found, chosen)


async def launch(pw: Playwright, options: BrowserOptions) -> Browser:
    """Запускает первый доступный браузер из лестницы; не нашлось ни одного — последняя ошибка."""
    ladder = channel_ladder(options.channel)
    last_error: PlaywrightError | None = None
    for channel in ladder:
        kwargs: dict[str, object] = {"headless": options.headless}
        if channel != BUNDLED:
            kwargs["channel"] = channel
        try:
            browser = await pw.chromium.launch(**kwargs)  # type: ignore[arg-type]
        except PlaywrightError as error:
            # Текст ошибки Playwright содержит путь с именем пользователя: в журнал — только тип
            last_error = error
            logger.info(
                "браузер %s не запустился: %s", NAMES.get(channel, channel), type(error).__name__
            )
            continue
        logger.info("браузер: %s", NAMES.get(channel, channel))
        return browser
    names = ", ".join(NAMES.get(c, c) for c in ladder)
    logger.error(
        "ни один браузер не запустился (%s): установите Google Chrome или выполните "
        "`uv run python dev.py setup --bot registro-civil-bot`",
        names,
    )
    assert last_error is not None  # лестница не бывает пустой
    raise last_error


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
