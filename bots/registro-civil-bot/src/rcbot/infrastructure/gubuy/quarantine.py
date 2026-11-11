"""Карантин неожиданных ответов (SPEC 2.3, 2.7): очищенный снимок для разбора, без cookies.

Пишутся только статус, тип содержимого и начало тела, пропущенное через редактор; заголовки не
пишутся вовсе. Хранится не больше 20 файлов (сроки — в очистке).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rcbot.infrastructure.logging.redaction import REDACTOR

MAX_FILES = 20
MAX_BODY_CHARS = 8000


def quarantine(
    directory: Path, at: datetime, source: str, status: int, content_type: str, body: str
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{at:%Y%m%dT%H%M%S%f}-{source}.json"
    record = {
        "at": at.isoformat(),
        "source": source,
        "status": status,
        "content_type": content_type,
        "body": REDACTOR.redact(body[:MAX_BODY_CHARS]),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    for old in sorted(directory.glob("*.json"))[:-MAX_FILES]:
        old.unlink(missing_ok=True)
    return path
