"""Sentry — выключен по умолчанию; если включён, без личных данных (SPEC 2.6)."""

from __future__ import annotations

import logging
from typing import Any

from rcbot.infrastructure.logging.redaction import REDACTOR

logger = logging.getLogger(__name__)

_MAX_DEPTH = 64


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise ValueError("событие слишком глубокое или с циклом")
    if isinstance(value, str):
        return REDACTOR.redact(value)
    if isinstance(value, dict):
        return {_scrub(k, depth + 1): _scrub(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(v, depth + 1) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return REDACTOR.redact(str(value))


def _before_send(event: Any, _hint: Any) -> Any:
    # Редактор применяется к каждой строке события, а не к тексту JSON: кавычки и косые черты
    # в секрете в JSON экранируются, и поиск по такому тексту их бы не нашёл. Событие, которое
    # нельзя очистить, отбрасывается целиком.
    try:
        return _scrub(event)
    except ValueError, RecursionError:
        return None


def init_sentry(enabled: bool, dsn: str, release: str) -> bool:
    if not enabled or not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        release=release,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=_before_send,
        traces_sample_rate=0.0,
    )
    logger.info("Sentry включён")
    return True
