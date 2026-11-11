"""Разведка (SPEC 6.2, D1): запись запросов шага «Agenda», чтобы написать `ApiSource`.

Браузер открывается с окном, вход выполняет владелец сам — бот паролей не вводит. Записываются
только ответы XHR/fetch с bpmgob и скрипт календаря; страницы целиком не пишутся (в них имя
владельца). Очистка — до записи на диск: без заголовков `Cookie`, `Set-Cookie`, `Authorization`;
номер документа, почта, телефоны и поля с личными данными в JSON заменяются на `***`.
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
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response, async_playwright

from rcbot.infrastructure.gubuy.browser import BrowserOptions, launch
from rcbot.infrastructure.gubuy.parsers import READ_MONTH_JS, month_view
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR

logger = logging.getLogger(__name__)

BPMGOB_HOST = "bpmgob.mec.gub.uy"
DROP_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization"}
PERSONAL_KEYS = re.compile(
    r"(?i)(nombre|apellido|name|mail|correo|tel[eé]fono|phone|celular|documento|"
    r"c[eé]dula|pasaporte|passport|usuario|user|direcci[oó]n|address)"
)
MAX_BODY_CHARS = 200_000
API_MARKER = "agenda_sae_api_disponibilidades"


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: REDACTOR.redact(v) for k, v in headers.items() if k.lower() not in DROP_HEADERS}


def _mask_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (MASK if PERSONAL_KEYS.search(str(k)) else _mask_json(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_json(v) for v in value]
    if isinstance(value, str):
        return REDACTOR.redact(value)
    return value


def clean_body(content_type: str, body: str) -> Any:
    if "json" in content_type.lower():
        try:
            return _mask_json(json.loads(body))
        except ValueError:
            pass
    return REDACTOR.redact(body[:MAX_BODY_CHARS])


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
                "url": REDACTOR.redact(url),
                "resource": request.resource_type,
                "status": response.status,
                "request_headers": _clean_headers(await request.all_headers()),
                "post_data": REDACTOR.redact(request.post_data or ""),
                "response_headers": _clean_headers(await response.all_headers()),
                "content_type": content_type,
                "body": clean_body(content_type, body),
            }
        )

    def _on_response(self, response: Response) -> None:
        task = asyncio.create_task(self._collect(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def run(self, at: datetime, wait_login_sec: int = 600) -> DiscoveryResult:
        async with async_playwright() as pw:
            browser = await launch(pw, self._options)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                page.on("response", self._on_response)
                await page.goto(self._login_url, wait_until="domcontentloaded")
                print("Войдите в аккаунт в открывшемся окне. Бот ждёт и ничего не вводит сам…")
                await page.wait_for_url(
                    lambda url: BPMGOB_HOST in url and "/autenticacion" not in url,
                    timeout=wait_login_sec * 1000,
                )
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
