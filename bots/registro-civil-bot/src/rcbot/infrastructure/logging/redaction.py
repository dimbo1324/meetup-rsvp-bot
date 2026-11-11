"""Вырезание секретов и личных данных из логов и событий Sentry (SPEC 2.6).

Два уровня защиты: точные значения секретов, прочитанные из keyring (регистрируются при
чтении), и шаблоны того, что может прийти извне — заголовки cookies и авторизации, токены
Telegram, адреса почты, номера телефонов и документов.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable

MASK = "***"
_MIN_SECRET_LEN = 4

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(cookie|set-cookie|authorization)\b(\s*[:=]\s*)[^\r\n]+"), rf"\1\2{MASK}"),
    (re.compile(r"(?i)\b(simple_bpm_[\w-]*|TS0[0-9a-f]+|target_bal)=[^;\s,'\"]+"), rf"\1={MASK}"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), MASK),
    (re.compile(r"(?i)\bbot\d{6,12}:[A-Za-z0-9_-]{30,}"), f"bot{MASK}"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), MASK),
    # Телефоны с кодом страны и длинные цифровые номера (документы, chat_id); даты вида
    # 2027-02-23 и время не задеваются
    (re.compile(r"\+\d[\d\s-]{7,}\d"), MASK),
    (re.compile(r"(?<![\d.:-])\d{9,}(?![\d.:-])"), MASK),
    (
        re.compile(r"(?i)\b(password|contrase[nñ]a|passwd|token|secret)\b(\s*[:=]\s*)\S+"),
        rf"\1\2{MASK}",
    ),
)


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, value: str) -> None:
        value = value.strip()
        if len(value) >= _MIN_SECRET_LEN:
            with self._lock:
                self._secrets.add(value)

    def register_many(self, values: Iterable[str]) -> None:
        for value in values:
            self.register(value)

    def redact(self, text: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


REDACTOR = Redactor()


class RedactionFilter(logging.Filter):
    """Переписывает сообщение и трассировку записи до того, как её увидит любой обработчик."""

    def __init__(self, redactor: Redactor = REDACTOR) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact(record.getMessage())
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            formatter = logging.Formatter()
            record.exc_text = self._redactor.redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)
        return True
