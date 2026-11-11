"""Sentry — выключен по умолчанию; если включён, без личных данных (SPEC 2.6)."""

from __future__ import annotations

import json
import logging
from typing import Any

from rcbot.infrastructure.logging.redaction import REDACTOR

logger = logging.getLogger(__name__)


def _before_send(event: Any, _hint: Any) -> Any:
    # Событие целиком проходит через тот же редактор, что и логи
    try:
        return json.loads(REDACTOR.redact(json.dumps(event, default=str)))
    except TypeError, ValueError:
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
