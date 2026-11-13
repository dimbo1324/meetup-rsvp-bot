"""Sentry — выключен по умолчанию; если включён, без личных данных (SPEC 2.6)."""

from __future__ import annotations

import logging
from typing import Any

from rcbot.infrastructure.logging.redaction import MASK, REDACTOR, SECRET_KEY_NAMES

logger = logging.getLogger(__name__)

_MAX_DEPTH = 64
SERVER_NAME = "rcbot"


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise ValueError("событие слишком глубокое или с циклом")
    if isinstance(value, str):
        return REDACTOR.redact(value)
    if isinstance(value, dict):
        return {
            _scrub(k, depth + 1): (
                MASK
                if isinstance(k, str) and SECRET_KEY_NAMES.search(k) and v is not None
                else _scrub(v, depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub(v, depth + 1) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        # Число проверяется как текст, как в журнале: длинный номер (chat_id) становится `***`
        text = str(value)
        redacted = REDACTOR.redact(text)
        return value if redacted == text else redacted
    return REDACTOR.redact(str(value))


def _before_send(event: Any, _hint: Any) -> Any:
    # Редактор применяется к каждой строке события, а не к тексту JSON: кавычки и косые черты
    # в секрете в JSON экранируются, и поиск по такому тексту их бы не нашёл. Событие, которое
    # нельзя очистить, отбрасывается целиком.
    try:
        return _scrub(event)
    except Exception:  # объект с падающим repr и любая другая беда: не отправлять
        logger.warning("событие Sentry отброшено: очистить не удалось")
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
        # Без имени компьютера: по умолчанию SDK подставляет hostname
        server_name=SERVER_NAME,
    )
    logger.info("Sentry включён")
    return True
