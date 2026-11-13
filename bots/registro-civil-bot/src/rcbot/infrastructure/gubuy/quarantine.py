"""Карантин неожиданных ответов (SPEC 2.3, 2.7): очищенный снимок для разбора, без cookies.

Пишутся только статус и тип содержимого; тело — только у ответа API (JSON) и скрипта, и то
начало, пропущенное через редактор. Страница (HTML, текст, неизвестный тип) не пишется вовсе:
в ней имя и документ владельца, которых редактор не знает, — вместо неё длина, sha256 и вид
страницы. Заголовки не пишутся. Хранится не больше 20 файлов (сроки — в очистке).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from itertools import count
from pathlib import Path

from rcbot.infrastructure.gubuy import MAX_STORED_FILES
from rcbot.infrastructure.gubuy.pages import classify
from rcbot.infrastructure.logging.redaction import REDACTOR

MAX_FILES = MAX_STORED_FILES
MAX_BODY_CHARS = 8000
# Тело пишется только у этих типов: ответ API и скрипт календаря нужны для разбора, и личных
# данных в них нет по устройству сайта. Всё прочее — страница, её тело не сохраняется
_KEPT_TYPES = ("json", "javascript", "ecmascript")


def keeps_body(content_type: str, body: str) -> bool:
    """Тело можно сохранить (через редактор): JSON или скрипт, и это не разметка страницы.

    Разметка с типом JSON — страница входа или ошибки, отданная сайтом под чужим типом.
    """
    lowered = content_type.lower()
    return any(kind in lowered for kind in _KEPT_TYPES) and not body.lstrip().startswith("<")


def body_digest(body: str, url: str = "", status: int | None = None) -> dict[str, object]:
    """Сведения о теле вместо самого тела: по ним видно, что пришло, но не что в нём написано."""
    return {
        "omitted": True,
        "chars": len(body),
        "sha256": hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest(),
        "page": str(classify(url, body, status)),
    }


def _free_path(directory: Path, at: datetime, source: str) -> Path:
    stamp = f"{at:%Y%m%dT%H%M%S%f}"
    path = directory / f"{stamp}-{source}.json"
    # Два ответа в один и тот же момент не должны затирать друг друга: берём следующий номер
    for number in count(1):
        if not path.exists():
            return path
        path = directory / f"{stamp}-{number}-{source}.json"
    raise AssertionError("недостижимо")  # pragma: no cover


def quarantine(
    directory: Path,
    at: datetime,
    source: str,
    status: int,
    content_type: str,
    body: str,
    *,
    url: str = "",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = _free_path(directory, at, source)
    record = {
        "at": at.isoformat(),
        "source": source,
        "status": status,
        "content_type": REDACTOR.redact(content_type),
        "body": (
            REDACTOR.redact_truncated(body, MAX_BODY_CHARS)
            if keeps_body(content_type, body)
            else body_digest(body, url, status)
        ),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    for old in sorted(directory.glob("*.json"))[:-MAX_FILES]:
        old.unlink(missing_ok=True)
    return path
