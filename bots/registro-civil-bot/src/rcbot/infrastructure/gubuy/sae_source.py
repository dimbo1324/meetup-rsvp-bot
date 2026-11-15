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
from html.parser import HTMLParser
from pathlib import Path

import aiohttp

from rcbot.application.ports import Clock
from rcbot.domain.errors import RateLimited, SiteUnavailable, UnexpectedResponse
from rcbot.domain.models import AvailabilitySnapshot, SourceKind, WatchTarget
from rcbot.infrastructure.gubuy import MAX_STORED_FILES
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.logging.redaction import REDACTOR

logger = logging.getLogger(__name__)

# Сравнение идёт по видимому тексту без регистра, ударений и лишних пробелов
NO_SLOTS_PHRASE = "no hay cupos disponibles"
# Страница записи узнаётся по выбору офиса («Ubicación»); названия кнопок здесь не пишем —
# архитектурный тест запрещает их в строках gubuy
BOOKING_PAGE_MARKERS = ("ubicacion",)
SNAPSHOT_PREFIX = "sae-"
# Статусы отказа защитного шлюза: шаг назад по лестнице частоты (D4)
RATE_LIMIT_STATUSES = frozenset({403, 429})
# Предел ответа (после распаковки): страница записи — десятки килобайт. Больший ответ не читается
# в память целиком и считается неожиданным
MAX_RESPONSE_BYTES = 1024 * 1024
_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template"})


# Элементы без закрывающего тега: в стеке открытых элементов их не держим
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)


def _hidden(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    """Скрипты, стили и скрытые элементы: надпись «нет мест» может остаться в разметке скрытой."""
    if tag in _SKIPPED_TAGS:
        return True
    for name, value in attrs:
        if name == "hidden" or (name == "aria-hidden" and value == "true"):
            return True
        if name == "style" and value and "display:none" in value.replace(" ", "").lower():
            return True
    return False


class _VisibleText(HTMLParser):
    """Видимый текст: без скриптов, стилей и элементов, скрытых атрибутами или `display:none`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[tuple[str, bool]] = []

    def _inside_hidden(self) -> bool:
        return any(hidden for _, hidden in self._open)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID_TAGS:
            self._open.append((tag, _hidden(tag, attrs)))

    def handle_endtag(self, tag: str) -> None:
        # Незакрытые вложенные элементы закрываются вместе с родителем, как в браузере
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self._inside_hidden():
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


async def read_limited(response: aiohttp.ClientResponse) -> tuple[str, bool]:
    """Тело не длиннее `MAX_RESPONSE_BYTES` и признак «ответ больше предела»."""
    declared = response.content_length
    if declared is not None and declared > MAX_RESPONSE_BYTES:
        return "", True
    raw = bytearray()
    # read(n) отдаёт сколько есть в буфере, не обязательно n: читаем до конца или до предела
    while len(raw) <= MAX_RESPONSE_BYTES:
        chunk = await response.content.read(MAX_RESPONSE_BYTES + 1 - len(raw))
        if not chunk:
            break
        raw += chunk
    too_large = len(raw) > MAX_RESPONSE_BYTES
    return _decode(bytes(raw[:MAX_RESPONSE_BYTES]), response.charset), too_large


def _decode(raw: bytes, charset: str | None) -> str:
    """Как `response.text(errors="replace")`: кодировка из заголовка, без неё — UTF-8."""
    try:
        return raw.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


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
