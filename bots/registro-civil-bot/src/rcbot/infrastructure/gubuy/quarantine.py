"""Карантин неожиданных ответов (SPEC 2.3, 2.7): очищенный снимок для разбора, без cookies.

Пишутся только статус, тип содержимого и начало тела, пропущенное через редактор; заголовки не
пишутся вовсе. Хранится не больше 20 файлов (сроки — в очистке).
"""

from __future__ import annotations

import json
from datetime import datetime
from itertools import count
from pathlib import Path

from rcbot.infrastructure.logging.redaction import REDACTOR

MAX_FILES = 20
MAX_BODY_CHARS = 8000


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
    directory: Path, at: datetime, source: str, status: int, content_type: str, body: str
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = _free_path(directory, at, source)
    record = {
        "at": at.isoformat(),
        "source": source,
        "status": status,
        "content_type": content_type,
        # Сначала вырезаем секреты, потом усекаем: усечение до очистки оставило бы от значения,
        # разрезанного границей, префикс, который редактор уже не узнает
        "body": REDACTOR.redact(body)[:MAX_BODY_CHARS],
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    for old in sorted(directory.glob("*.json"))[:-MAX_FILES]:
        old.unlink(missing_ok=True)
    return path
