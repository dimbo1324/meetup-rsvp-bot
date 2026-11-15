"""`SaeSource` — главный источник (решение R12): публичная страница записи SAE, без входа.

Страница `sae.mec.gub.uy/sae/agendarReserva/Paso1.xhtml?e=9&a=82` открывается без аккаунта и без
контрольного вопроса. Бот только читает её (GET): ничего не нажимает и форм не отправляет.

Как страница выглядит, когда места есть, пока неизвестно. Поэтому первая версия различает три
состояния: надпись «no hay cupos disponibles» есть — мест нет; страница записи без этой надписи —
места есть, даты не разобраны (`dates_unknown`); не страница записи — неожиданный ответ. Снимок
страницы сохраняется при первой проверке и при каждой смене состояния: по нему потом пишется
разбор дат.
"""

from __future__ import annotations

import html
import logging
import time as monotonic_time
import unicodedata
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path

import aiohttp

from rcbot.application.ports import Clock, SessionCookie
from rcbot.domain.errors import RateLimited, SiteUnavailable, UnexpectedResponse
from rcbot.domain.models import AvailabilitySnapshot, SourceKind, WatchTarget
from rcbot.infrastructure.gubuy import MAX_STORED_FILES
from rcbot.infrastructure.gubuy.api_source import read_limited
from rcbot.infrastructure.gubuy.pages import RATE_LIMIT_STATUSES
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.logging.redaction import REDACTOR

logger = logging.getLogger(__name__)

# Сравнение идёт по видимому тексту без регистра, ударений и лишних пробелов
NO_SLOTS_PHRASE = "no hay cupos disponibles"
# Страница записи узнаётся по выбору офиса («Ubicación»); названия кнопок здесь не пишем —
# архитектурный тест запрещает их в строках gubuy
BOOKING_PAGE_MARKERS = ("ubicacion",)
SNAPSHOT_PREFIX = "sae-"
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})


class _VisibleText(HTMLParser):
    """Текст страницы без скриптов и стилей: шаблон надписи в скрипте не считается надписью."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def visible_text(page: str) -> str:
    """Видимый текст страницы в нижнем регистре, без ударений и с одиночными пробелами."""
    parser = _VisibleText()
    parser.feed(page)
    parser.close()
    text = html.unescape(" ".join(parser.parts)).lower()
    plain = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    return " ".join(plain.split())


def seats_open(page: str) -> bool | None:
    """`False` — мест нет, `True` — страница записи без надписи «нет мест», `None` — не та."""
    text = visible_text(page)
    if NO_SLOTS_PHRASE in text:
        return False
    if any(marker in text for marker in BOOKING_PAGE_MARKERS):
        return True
    return None


class SaeSource:
    def __init__(self, http: SafeHttp, url: str, snapshots_dir: Path, clock: Clock) -> None:
        self._http = http
        self._url = url
        self._snapshots_dir = snapshots_dir
        self._clock = clock
        self._last_state: bool | None = None
        self._saved_once = False

    @property
    def kind(self) -> SourceKind:
        return SourceKind.SAE

    def use_cookies(self, cookies: Sequence[SessionCookie]) -> None:
        """Страница SAE открывается без входа: cookies сессии bpmgob ей не нужны."""

    async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
        started = monotonic_time.perf_counter()
        try:
            async with self._http.request("GET", self._url) as response:
                status = response.status
                body, too_large = await read_limited(response)
        except (aiohttp.ClientError, TimeoutError) as error:
            raise SiteUnavailable(f"страница SAE недоступна: {type(error).__name__}") from None
        if status in RATE_LIMIT_STATUSES:
            raise RateLimited(f"SAE ответил {status}")
        if status >= 500:
            raise SiteUnavailable(f"SAE ответил {status}")
        if status != 200 or too_large:
            raise UnexpectedResponse(f"SAE ответил {status}, страница записи не получена")
        state = seats_open(body)
        if state is None:
            self._save(body, "unknown")
            raise UnexpectedResponse("страница SAE не распознана: нет признаков страницы записи")
        if not self._saved_once or state != self._last_state:
            self._save(body, "open" if state else "empty")
        self._last_state = state
        latency_ms = (monotonic_time.perf_counter() - started) * 1000
        return AvailabilitySnapshot(
            target.id,
            self._clock.now(),
            frozenset(),
            SourceKind.SAE,
            latency_ms,
            dates_unknown=state,
        )

    def _save(self, page: str, label: str) -> None:
        """Снимок публичной страницы для будущего разбора дат; сбой записи не мешает проверке."""
        self._saved_once = True
        moment = self._clock.now().strftime("%Y%m%dT%H%M%S")
        try:
            self._snapshots_dir.mkdir(parents=True, exist_ok=True)
            path = self._snapshots_dir / f"{SNAPSHOT_PREFIX}{moment}-{label}.html"
            path.write_text(REDACTOR.redact(page), encoding="utf-8", errors="replace")
            old = sorted(self._snapshots_dir.glob(f"{SNAPSHOT_PREFIX}*.html"))
            for stale in old[: max(0, len(old) - MAX_STORED_FILES)]:
                stale.unlink(missing_ok=True)
        except OSError as error:
            logger.warning("снимок страницы SAE не сохранён: %s", type(error).__name__)

    async def close(self) -> None:
        await self._http.close()
