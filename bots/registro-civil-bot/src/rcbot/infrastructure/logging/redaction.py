"""Вырезание секретов и личных данных из логов, журнала и событий Sentry (SPEC 2.6).

Два уровня защиты: точные значения секретов, прочитанные из keyring (регистрируются при
чтении), и шаблоны того, что может прийти извне — заголовки cookies и авторизации, токены
Telegram, адреса почты, номера телефонов и документов.

Свойства, на которые опираются остальные модули (проверяются в `tests/privacy/`):
- время работы линейно по длине текста: ни один шаблон не имеет вложенных неограниченных
  повторов (иначе строка из 60 тысяч символов без пробелов блокирует цикл событий на минуты);
- результат одного прохода не меняется от повторного (`redact(redact(x)) == redact(x)`), а
  зарегистрированное значение не переживает вырезание, даже если маска «склеила» его заново;
- текст, сериализованный в JSON (журнал, `status.json`, Sentry), остаётся корректным JSON:
  шаблоны не съедают кавычки и скобки, а точные значения вырезаются и в JSON-экранированном
  виде (кавычка, обратная косая черта, не-ASCII как `\\uXXXX`) и в виде для URL.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Iterable
from urllib.parse import quote, quote_plus

MASK = "***"
# Имена ключей, значения которых секретны целиком (заголовки, JSON, словари событий): их значение
# вырезается по ключу, потому что в самом значении («a=b; c=d») признаков секрета нет
SECRET_KEY_NAMES = re.compile(
    r"(?i)cookie|authoriz|passw|contrase|token|secret|api[-_]?key|csrf|xsrf|session"
)
_MIN_SECRET_LEN = 4
# Запас на секрет, разрезанный границей усечения (`Redactor.redact_truncated`): длиннее любого
# значения, которое редактор вырезает целиком
TRUNCATE_MARGIN = 1000

# Значение после «ключ=» или «"ключ": »: в кавычках — целиком; без них — до пробела или знака,
# который в JSON, URL и скобках уже не часть значения. Префикс Bearer/Basic входит в значение.
_VALUE = (
    r"""(?P<val>"(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*'|"""
    r"""(?:(?:bearer|basic)\s+)?[^\s"'\\,;&)\]}]+)"""
)
_SEP = r"""(?P<q>["']?)(?P<sep>\s*[:=]\s*)"""

# Имена, значения которых секретны, где бы они ни встретились: `access_token=…`, `"password": …`,
# `csrf_token=…`; имена cookies сессии bpmgob (SPEC 1.3). Без `\b` и без префиксов: так поиск
# остаётся линейным, а `access_` и `x-` просто остаются перед совпадением.
_KEY_VALUE = re.compile(
    r"(?i)(?:password|passwd|contrase[nñ]a|пароль|токен|секрет|token|secret|api[_-]?key|csrf|xsrf|session[_-]?id|"
    r"simple_bpm_[\w-]*|ts0[0-9a-f]+|target_bal)" + _SEP + _VALUE
)
# Заголовки: значение — до конца строки (в нём `;` и `=`), но не за кавычку JSON-строки
_HEADER = re.compile(
    r"""(?i)(?:cookie|authorization)"""
    + _SEP
    + r"""(?P<val>"(?:[^"\\\r\n]|\\.)*"|'[^'\r\n]*'|[^\r\n"\\]+)"""
)


def _mask_value(match: re.Match[str]) -> str:
    """Оставляет ключ и разделитель; значение в кавычках остаётся в кавычках (JSON цел)."""
    value, key_quote = match["val"], match["q"]
    head = match.group(0)[: match.start("val") - match.start()]
    if value[:1] in ("'", '"'):
        return f"{head}{value[0]}{MASK}{value[0]}"
    # Ключ в кавычках (JSON, repr словаря): значение без кавычек — число; кавычки ставим сами
    return f"{head}{key_quote}{MASK}{key_quote}" if key_quote else f"{head}{MASK}"


_JSON_ESCAPE = re.compile(r"u[0-9a-fA-F]{4}|.", re.DOTALL)


def _mask_email(match: re.Match[str]) -> str:
    # Адрес сразу после обратной косой черты — это JSON-экранирование (перенос строки, \uXXXX)
    # вместе с началом адреса: экранирование остаётся целым, иначе маска ломает JSON
    start = match.start()
    if start and match.string[start - 1] == "\\" and (esc := _JSON_ESCAPE.match(match.group())):
        return esc.group() + MASK
    return MASK


_PATTERNS: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...] = (
    (_HEADER, _mask_value),
    (_KEY_VALUE, _mask_value),
    # Токен Telegram (в том числе в адресе /bot<токен>/метод): 6–12 цифр, двоеточие, 30+ знаков.
    # Хвостовой `\b` нельзя: токен может кончаться на «-», и последний знак остался бы в тексте
    (re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"), MASK),
    # Адрес почты; начало только с границы слова — иначе длинная строка без «@» даёт O(n²)
    (re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+"), _mask_email),
    # Телефон с «+» и кодом страны: от 8 цифр, между ними пробелы, скобки, дефисы, точки
    (re.compile(r"\+\d(?:[\s().-]{0,2}\d){7,}"), MASK),
    # Длинные номера (документы, chat_id): 9 и больше цифр подряд. Не задеваются дробная часть
    # (`12.123456789`), число с дробью (`123456789.5`), даты и время. Десятизначные секунды epoch
    # вырезаются сознательно: бот пишет время ISO, а номер документа важнее такой метки
    (re.compile(r"(?<!\d)(?<!\d[.:])\d{9,}(?!\d)(?![.:-]\d)"), MASK),
)


def _variants(value: str) -> set[str]:
    """Значение в тех видах, в каких оно встретится в JSON-тексте и в адресе запроса."""
    return {
        value,
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        quote(value, safe=""),
        quote_plus(value),
    }


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._ordered: list[str] = []
        self._lock = threading.Lock()

    def register(self, value: str) -> None:
        """Значения короче четырёх знаков не регистрируются: «1234» исказил бы любой лог."""
        value = value.strip()
        if len(value) < _MIN_SECRET_LEN:
            return
        with self._lock:
            self._secrets |= _variants(value)
            # Длинные раньше коротких: короткое значение не должно отщипнуть кусок длинного
            self._ordered = sorted(self._secrets, key=len, reverse=True)

    def clear(self) -> None:
        """Забыть зарегистрированные значения: изоляция тестов."""
        with self._lock:
            self._secrets.clear()
            self._ordered = []

    def register_many(self, values: Iterable[str]) -> None:
        for value in values:
            self.register(value)

    def redact(self, text: str) -> str:
        with self._lock:
            secrets = self._ordered
        # Каждая замена укорачивает текст (значение ≥ 4 знаков, маска — 3), поэтому цикл
        # конечен; повтор нужен, когда маска «склеивается» с соседним текстом в то же значение
        changed = True
        while changed:
            changed = False
            for secret in secrets:
                if secret in text:
                    text = text.replace(secret, MASK)
                    changed = True
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def redact_truncated(self, text: str, limit: int) -> str:
        """Очищенный текст не длиннее `limit` знаков.

        Сначала вырезаются секреты, потом текст усекается: усечение до очистки оставило бы от
        значения, разрезанного границей, префикс, который редактор уже не узнает. Редактору
        отдаётся `limit` плюс запас, а не весь текст: на длинной строке без разделителей шаблоны
        работают квадратично, а усечённое пишется прямо в цикле опроса.
        """
        return self.redact(text[: limit + TRUNCATE_MARGIN])[:limit]


REDACTOR = Redactor()


class RedactionFilter(logging.Filter):
    """Переписывает сообщение и трассировку записи до того, как её увидит любой обработчик."""

    def __init__(self, redactor: Redactor = REDACTOR) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except TypeError, ValueError, KeyError, IndexError:
            # Неверные аргументы формата не должны ронять вызывающий код (logging их глотает сам,
            # но фильтр форматирует раньше него): пишем шаблон и аргументы как есть
            message = f"{record.msg} {record.args}"
        record.msg = self._redactor.redact(message)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            formatter = logging.Formatter()
            record.exc_text = self._redactor.redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redactor.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor.redact(record.stack_info)
        return True
