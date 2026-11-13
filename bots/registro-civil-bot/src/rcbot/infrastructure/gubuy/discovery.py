"""Разведка (SPEC 6.2, D1): запись запросов шага «Agenda», чтобы написать `ApiSource`.

Браузер открывается с окном, вход выполняет владелец сам — бот паролей не вводит. Записываются
только ответы XHR/fetch с bpmgob и скрипт календаря. Тело пишется только у JSON и скрипта;
HTML, текст и прочее — только длина, sha256 и вид страницы (в странице имя владельца). Очистка —
до записи на диск: без заголовков `Cookie`, `Set-Cookie`, `Authorization`; номер документа,
почта, телефоны и поля с личными данными в JSON заменяются на `***`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response, async_playwright

from rcbot.infrastructure.gubuy.browser import BrowserOptions, launch
from rcbot.infrastructure.gubuy.pages import BPMGOB_HOST, is_bpmgob_session_url
from rcbot.infrastructure.gubuy.parsers import READ_MONTH_JS, month_view
from rcbot.infrastructure.gubuy.quarantine import body_digest, keeps_body
from rcbot.infrastructure.logging.redaction import (
    MASK,
    REDACTOR,
    SECRET_KEY_NAMES,
    TRUNCATE_MARGIN,
)

logger = logging.getLogger(__name__)

# Заголовки сессии, авторизации и защиты от подделки запросов в дамп не попадают вовсе
DROP_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-csrf-token",
    "x-xsrf-token",
    "x-api-key",
}
# То же по шаблону: вариации имён (`X-CSRFToken`, `Api-Key`) не должны проскочить
_DROP_PATTERN = re.compile(r"(?i)cookie|authoriz|csrf|xsrf|api[-_]?key|token")
# Ключи JSON, значения которых секретны (CSRF нужен для разбора запроса, но не его значение)
SECRET_KEYS = SECRET_KEY_NAMES
PERSONAL_KEYS = re.compile(
    r"(?i)(nombre|apellido|name|mail|correo|tel[eé]fono|phone|celular|documento|"
    r"c[eé]dula|pasaporte|passport|usuario|user|direcci[oó]n|address)"
)
MAX_BODY_CHARS = 200_000
# Глубже этого вложения значения JSON не разбираются, а заменяются маской: рекурсия маскирования
# не должна упираться в предел интерпретатора на враждебном ответе
MAX_JSON_DEPTH = 64
API_MARKER = "agenda_sae_api_disponibilidades"


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: REDACTOR.redact(v)
        for k, v in headers.items()
        if k.lower() not in DROP_HEADERS and not _DROP_PATTERN.search(k)
    }


def _mask_json(value: Any, depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        return MASK
    if isinstance(value, dict):
        return {
            # Сам ключ тоже может быть личным («адрес почты → данные»): прогоняем и его
            REDACTOR.redact(str(k)): (
                MASK
                if PERSONAL_KEYS.search(str(k)) or SECRET_KEYS.search(str(k))
                else _mask_json(v, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_json(v, depth + 1) for v in value]
    if isinstance(value, str):
        return REDACTOR.redact(value)
    return value


_NOT_JSON = object()
_UNSAFE_JSON = object()


def _masked_json(text: str) -> Any:
    """Разобранный и замаскированный JSON; `_NOT_JSON`, если это не JSON.

    Длина проверяется до разбора: `json.loads` строит весь объект в памяти. Слишком длинный или
    слишком глубокий для разбора текст — `_UNSAFE_JSON`: он не пишется, только сведения о нём.
    """
    if len(text) > MAX_BODY_CHARS:
        return _UNSAFE_JSON
    try:
        return _mask_json(json.loads(text))
    except RecursionError:
        return _UNSAFE_JSON
    except ValueError:
        return _NOT_JSON


def clean_body(content_type: str, body: str, *, url: str = "", status: int | None = None) -> Any:
    """JSON — с маской личных ключей, скрипт — через редактор; страница — только сведения о ней."""
    if not body:
        return ""
    if not keeps_body(content_type, body):
        return body_digest(body, url, status)
    if "json" in content_type.lower():
        masked = _masked_json(body)
        if masked is _UNSAFE_JSON:
            return body_digest(body, url, status)
        if masked is not _NOT_JSON:
            return masked
    return REDACTOR.redact_truncated(body, MAX_BODY_CHARS)


def _mask_pairs(query: str) -> str:
    """`a=1&nombre=Ana` → `a=1&nombre=***`: значения личных полей формы и адреса."""
    parts = []
    for part in query.split("&"):
        key, sep, _ = part.partition("=")
        parts.append(f"{key}={MASK}" if sep and PERSONAL_KEYS.search(unquote_plus(key)) else part)
    return "&".join(parts)


def clean_url(url: str) -> str:
    base, mark, query = url.partition("?")
    return REDACTOR.redact(base + mark + _mask_pairs(query))


def clean_post_data(data: str) -> Any:
    """Тело запроса: JSON чистится как ответ, форма — по именам полей, прочее — редактором.

    Слишком длинное или глубокое тело не пишется: только сведения о нём, как о странице.
    """
    masked = _masked_json(data)
    if masked is _UNSAFE_JSON:
        return body_digest(data)
    if masked is not _NOT_JSON:
        return json.dumps(masked, ensure_ascii=False)
    return REDACTOR.redact_truncated(
        _mask_pairs(data[: MAX_BODY_CHARS + TRUNCATE_MARGIN]), MAX_BODY_CHARS
    )


@dataclass
class DiscoveryResult:
    path: Path
    records: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> list[str]:
        lines = []
        for r in self.records:
            mark = "  <== API календаря" if API_MARKER in r["url"] else ""
            lines.append(f"{r['method']:4} {r['status']} {r['resource']:6} {r['url']}{mark}")
        if any("socket.io" in r["url"] for r in self.records):
            lines.append("Замечен socket.io: календарь может обновляться без перезагрузки.")
        return lines


class DiscoveryRecorder:
    def __init__(
        self,
        login_url: str,
        agenda_url: str,
        location_text: str,
        months_ahead: int,
        out_dir: Path,
        options: BrowserOptions,
    ) -> None:
        self._login_url = login_url
        self._agenda_url = agenda_url
        self._location = location_text
        self._months = months_ahead
        self._out_dir = out_dir
        self._options = options
        self._records: list[dict[str, Any]] = []
        self._pending: set[asyncio.Task[None]] = set()

    async def _collect(self, response: Response) -> None:
        request = response.request
        url = response.url
        is_calendar_script = "calendario" in url and url.split("?")[0].endswith(".js")
        if urlparse(url).hostname != BPMGOB_HOST:
            return
        if request.resource_type not in ("xhr", "fetch") and not is_calendar_script:
            return
        try:
            body = await response.text()
        except PlaywrightError:
            body = ""
        content_type = response.headers.get("content-type", "")
        self._records.append(
            {
                "method": request.method,
                "url": clean_url(url),
                "resource": request.resource_type,
                "status": response.status,
                "request_headers": _clean_headers(await request.all_headers()),
                "post_data": clean_post_data(request.post_data or ""),
                "response_headers": _clean_headers(await response.all_headers()),
                "content_type": content_type,
                "body": clean_body(content_type, body, url=url, status=response.status),
            }
        )

    def _on_response(self, response: Response) -> None:
        task = asyncio.create_task(self._collect(response))
        self._pending.add(task)
        task.add_done_callback(self._collected)

    def _collected(self, task: asyncio.Task[None]) -> None:
        self._pending.discard(task)
        # Сбой записи одного ответа (страница закрылась, тело не читается) не должен пропадать
        # молча и не должен мешать остальным
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("разведка: ответ не записан: %s", type(error).__name__)

    async def run(self, at: datetime, wait_login_sec: int = 600) -> DiscoveryResult:
        async with async_playwright() as pw:
            browser = await launch(pw, self._options)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                page.on("response", self._on_response)
                await page.goto(self._login_url, wait_until="domcontentloaded")
                logger.info(
                    "Войдите в аккаунт в открывшемся окне. Бот ждёт и ничего не вводит сам…"
                )
                await page.wait_for_url(is_bpmgob_session_url, timeout=wait_login_sec * 1000)
                await page.goto(self._agenda_url, wait_until="networkidle")
                await self._walk_calendar(page)
                await page.wait_for_load_state("networkidle")
                if self._pending:
                    await asyncio.gather(*self._pending, return_exceptions=True)
            finally:
                await browser.close()
        return self._write(at)

    async def _walk_calendar(self, page: Page) -> None:
        """Выбор офиса и перелистывание месяцев — те же действия только чтения, что в DomSource."""
        radio = page.get_by_role("radio", name=re.compile(re.escape(self._location), re.I))
        if await radio.count() and not await radio.first.is_checked():
            await radio.first.check()
        await page.wait_for_selector(".ui-datepicker-calendar", state="visible")
        for _ in range(self._months - 1):
            view = month_view(await page.evaluate(READ_MONTH_JS))
            if not view.has_next:
                break
            await page.locator(".ui-datepicker-next").first.click()
            await page.wait_for_load_state("networkidle")

    def _write(self, at: datetime) -> DiscoveryResult:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        path = self._out_dir / f"discovery-{at:%Y%m%dT%H%M%S}.json"
        payload = {"recorded_at": at.isoformat(), "records": self._records}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("разведка: записано %d запросов в %s", len(self._records), path.name)
        return DiscoveryResult(path, self._records)
