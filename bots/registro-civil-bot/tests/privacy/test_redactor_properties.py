"""Свойства редактора секретов (SPEC 2.6, 7): то, что должно выполняться на ЛЮБОМ тексте.

Здесь не примеры, а инварианты: зарегистрированное значение не переживает вырезание ни в каком
окружении, результат устойчив к повтору, JSON остаётся JSON, время растёт линейно. Примеры
конкретных шаблонов (cookies, токены, телефоны) — в `test_redactor_patterns.py`.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from rcbot.infrastructure.logging.redaction import MASK, Redactor

# Значение секрета после strip() должно остаться длиной ≥ 4: только такие регистрируются
SECRET = st.text(min_size=4, max_size=40).map(str.strip).filter(lambda s: len(s) >= 4)

# Фрагменты, которые провоцируют шаблоны: из них собирается «реалистичный» грязный текст
TG_TOKEN = "123456789:" + "AbC_dEf-" * 5
FRAGMENTS = [
    "Cookie: a=b; c=d",
    "set-cookie: simple_bpm_session=zz",
    "Authorization: Bearer q1w2e3",
    "simple_bpm_session=abc",
    "TS0191f197=zz",
    "target_bal=9",
    "token=abc",
    "password: 'a b'",
    '{"token": "x"}',
    f"/bot{TG_TOKEN}/getMe",
    "user@example.com",
    "+598 99 123 456",
    "1234567890",
    "2027-02-23",
    "21:00:07.123456",
    "127.0.0.1:8080",
    "123456789.5",
    "\n",
    " ",
    "***",
    "x",
    "пароль=секрет",
]
NASTY_SECRETS = [
    'pa"ss\\w0rd',
    "пароль-Ünï😀",
    "a/b+c=d&e",
    "tab\there",
    "a***",
    ".*+?[](){}|^$",
    "q'q;q",
]

dirty_text = st.lists(st.sampled_from([*FRAGMENTS, *NASTY_SECRETS]), max_size=14).map("".join)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


@example(secret="a***", before="a", after="")
@example(secret=r".*+?[](){}|^$\\", before="x", after="y")
@example(secret="пароль-Ünï😀", before="é", after="ß")
@example(secret="abab", before="ab", after="ab")
@given(secret=SECRET, before=st.text(max_size=60), after=st.text(max_size=60))
def test_зарегистрированное_значение_не_переживает_вырезание(
    secret: str, before: str, after: str
) -> None:
    redactor = Redactor()
    redactor.register(secret)
    assert secret not in redactor.redact(before + secret + after)


@given(secret=SECRET, copies=st.integers(2, 6), glue=st.text(max_size=5))
def test_каждое_повторное_вхождение_вырезается(secret: str, copies: int, glue: str) -> None:
    redactor = Redactor()
    redactor.register(secret)
    assert secret not in redactor.redact(glue.join([secret] * copies))


@given(
    secrets=st.lists(st.text(alphabet="abc12", min_size=4, max_size=9), min_size=2, max_size=6),
    pieces=st.lists(st.integers(0, 7), min_size=1, max_size=12),
    order=st.randoms(use_true_random=False),
)
def test_пересекающиеся_секреты_не_переживают_в_любом_порядке_регистрации(
    secrets: list[str], pieces: list[int], order: Any
) -> None:
    """Секреты из одного алфавита часто являются подстроками друг друга и склеиваются в тексте."""
    pool = [*secrets, "", "a", "1", "ab"]
    text = "".join(pool[i % len(pool)] for i in pieces)
    shuffled = list(secrets)
    order.shuffle(shuffled)
    redactor = Redactor()
    redactor.register_many(shuffled)
    cleaned = redactor.redact(text)
    assert [s for s in secrets if s in cleaned] == []


def test_длинное_значение_вырезается_раньше_короткого() -> None:
    """Иначе короткое «abcd» отщипнуло бы начало «abcd1234» и хвост «1234» остался бы в логе."""
    for order in (("abcd", "abcd1234"), ("abcd1234", "abcd")):
        redactor = Redactor()
        redactor.register_many(order)
        assert redactor.redact("x abcd1234 y abcd z") == f"x {MASK} y {MASK} z"


@given(
    value=st.text(alphabet="abcdef ", max_size=3),
    text=st.text(alphabet="abcdef xyz", max_size=40),
)
def test_значения_короче_четырёх_знаков_не_регистрируются(value: str, text: str) -> None:
    """Осознанно: «123» или «ab» исказили бы каждый лог; защита таких значений — не задача."""
    redactor = Redactor()
    redactor.register(value)
    assert redactor.redact(text) == text


@pytest.mark.parametrize("value", ["", "abc", "   abc   ", "\t\n  ab  \n", "          "])
def test_короткое_и_пробельное_значение_не_меняет_текст(value: str) -> None:
    redactor = Redactor()
    redactor.register(value)
    assert redactor.redact("abc  ab  a b\tc") == "abc  ab  a b\tc"


def test_граница_длины_четыре_знака() -> None:
    redactor = Redactor()
    redactor.register("abc")
    redactor.register("abcd")
    assert redactor.redact("abc abcd") == f"abc {MASK}"


def test_значение_регистрируется_после_strip() -> None:
    """Секрет из keyring вставлен с пробелом на конце, а в лог попадёт без него."""
    redactor = Redactor()
    redactor.register("  hunter2hunter  \n")
    assert redactor.redact("p=hunter2hunter.") == f"p={MASK}."


def test_clear_забывает_значения() -> None:
    redactor = Redactor()
    redactor.register("hunter2hunter")
    redactor.clear()
    assert redactor.redact("hunter2hunter") == "hunter2hunter"


@given(text=dirty_text, secrets=st.lists(st.sampled_from(NASTY_SECRETS), max_size=3))
def test_повторное_вырезание_ничего_не_меняет(text: str, secrets: list[str]) -> None:
    redactor = Redactor()
    redactor.register_many(secrets)
    once = redactor.redact(text)
    assert redactor.redact(once) == once


@given(text=dirty_text, secrets=st.lists(st.sampled_from(NASTY_SECRETS), max_size=3))
def test_после_вырезания_нет_ни_одного_зарегистрированного_значения(
    text: str, secrets: list[str]
) -> None:
    redactor = Redactor()
    redactor.register_many(secrets)
    cleaned = redactor.redact(text)
    assert [s for s in secrets if s in cleaned] == []


@pytest.mark.parametrize("ensure_ascii", [True, False])
@given(
    values=st.lists(dirty_text | st.text(max_size=30), min_size=1, max_size=5),
    keys=st.lists(st.sampled_from(["token", "Cookie", "password", "msg", "a b", "пароль"])),
)
@settings(max_examples=150)
def test_json_после_вырезания_остаётся_json_и_без_секретов(
    ensure_ascii: bool, values: list[str], keys: list[str]
) -> None:
    """Журнал, status.json и Sentry режут уже сериализованный JSON: он не должен ломаться."""
    redactor = Redactor()
    redactor.register_many(NASTY_SECRETS)
    data: dict[str, Any] = {
        f"{k}{i}" if i % 2 else k: v for i, (k, v) in enumerate(zip(keys, values, strict=False))
    }
    data["list"] = values
    data["nested"] = {"x": values[0], "n": 7}
    text = json.dumps(data, ensure_ascii=ensure_ascii)
    loaded = json.loads(redactor.redact(text))
    leaked = [s for s in NASTY_SECRETS if any(s in v for v in _strings(loaded))]
    assert leaked == []


def test_ключ_в_кавычках_с_числом_даёт_корректный_json() -> None:
    redactor = Redactor()
    assert json.loads(redactor.redact('{"token": 123456, "ok": 1}')) == {"token": MASK, "ok": 1}


# --- время работы ---

LONG_TEXTS = {
    "слово без пробелов": "a" * 30_000,
    "дефисы": "a-" * 15_000,
    "подчёркивания": "a_" * 15_000,
    "цифры": "1" * 30_000,
    "цифры с пробелами": "1 " * 15_000,
    "плюсы": "+1" * 15_000,
    "собаки": "a@" * 15_000,
    "точки": "a." * 15_000,
    "двоеточия": "1:" * 15_000,
    "base64-блоб": "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 800,
    "слово и «@» в конце": "x" * 30_000 + "@",
    "ключи без значений": "token " * 5_000 + "cookie " * 5_000,
}


@pytest.mark.parametrize("name", list(LONG_TEXTS))
def test_время_вырезания_линейно_длинная_строка_не_подвешивает_логирование(name: str) -> None:
    """Логи пишутся синхронно в цикле событий: строка в 30 КБ не должна занимать его на секунды."""
    redactor = Redactor()
    redactor.register("hunter2hunter")
    text = LONG_TEXTS[name]
    started = time.perf_counter()
    redactor.redact(text)
    assert time.perf_counter() - started < 1.0


def test_мегабайт_текста_с_пятьюдесятью_секретами_быстрее_секунды() -> None:
    secrets = [f"Sec{i:02d}-valuE-{i * 7919 % 10007:05d}" for i in range(50)]
    redactor = Redactor()
    redactor.register_many(secrets)
    line = (
        "2026-11-12 15:00:00 INFO rcbot.poll: проверка прошла за 312 мс, мест нет; состояние ok\n"
    )
    parts = []
    size = 0
    index = 0
    while size < 1_000_000:
        piece = line if index % 40 else f"вход: {secrets[(index // 40) % 50]} принят\n"
        parts.append(piece)
        size += len(piece)
        index += 1
    text = "".join(parts)
    best = min(_timed(redactor, text) for _ in range(3))
    assert best < 1.0
    cleaned = redactor.redact(text)
    assert [s for s in secrets if s in cleaned] == []


def _timed(redactor: Redactor, text: str) -> float:
    started = time.perf_counter()
    redactor.redact(text)
    return time.perf_counter() - started
