"""Шаблоны редактора: что вырезается без регистрации и что НЕ должно искажаться (SPEC 2.6, 7).

Все значения придуманы. Ложные срабатывания проверены так же строго, как утечки: фильтр,
который портит даты и время, делает лог бесполезным для разбора ночной тревоги.
"""

from __future__ import annotations

import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.infrastructure.logging.redaction import MASK, Redactor

TOKEN = "123456789:" + "AbCdEfGhIj_KlMnOpQrStUvWxYz-0123456"
redactor = Redactor()


def clean(text: str) -> str:
    return redactor.redact(text)


# --- cookies и заголовки ---


@pytest.mark.parametrize(
    "header",
    [
        "Cookie",
        "COOKIE",
        "cookie",
        "Set-Cookie",
        "SET-COOKIE",
        "set-cookie",
        "Authorization",
        "AUTHORIZATION",
        "authorization",
        "Proxy-Authorization",
    ],
)
@pytest.mark.parametrize("sep", [": ", ":", " : ", "=", "= "])
def test_заголовки_cookie_и_authorization_вырезаются_в_любом_регистре(
    header: str, sep: str
) -> None:
    secret = "zZ9Qq-sid_VALUE.42; path=/; a=b%3D"
    out = clean(f"запрос {header}{sep}{secret}")
    assert "VALUE" not in out
    assert "path=/" not in out
    assert header in out
    assert out.startswith("запрос ")


def test_значение_заголовка_режется_до_конца_строки_и_не_дальше() -> None:
    text = "Cookie: a=b; c=d\nHost: bpmgob.mec.gub.uy\nAuthorization: Bearer abcdef\nконец"
    assert clean(text) == f"Cookie: {MASK}\nHost: bpmgob.mec.gub.uy\nAuthorization: {MASK}\nконец"


def test_заголовки_в_json_и_repr_словаря_вырезаются() -> None:
    assert clean('{"Cookie": "a=b; c=d", "Host": "x"}') == f'{{"Cookie": "{MASK}", "Host": "x"}}'
    assert clean("{'Authorization': 'Bearer qwerty', 'Host': 'x'}") == (
        f"{{'Authorization': '{MASK}', 'Host': 'x'}}"
    )


@pytest.mark.parametrize(
    "cookie",
    [
        "simple_bpm_session",
        "simple_bpm_saml_s",
        "simple_bpm_login_c",
        "SIMPLE_BPM_LOCATION",
        "simple_bpm_query",
        "target_bal",
        "TS0191f197",
        "TS01d82af7",
        "ts01d82af7",
    ],
)
def test_cookies_сайта_вырезаются_по_имени(cookie: str) -> None:
    for text in (f"{cookie}=Zm9vYmFy+/=", f"x {cookie}: 'Zm9vYmFy'", f'{{"{cookie}": "Zm9vYmFy"}}'):
        out = clean(text)
        assert "Zm9vYmFy" not in out
        assert cookie in out


@pytest.mark.parametrize(
    "line",
    [
        "password=hunter2hunter",
        "PASSWORD: hunter2hunter",
        "token=hunter2hunter",
        "secret = hunter2hunter",
        "contraseña=hunter2hunter",
        "Contrasena: hunter2hunter",
        "пароль: hunter2hunter",
        "access_token=hunter2hunter",
        "csrf_token=hunter2hunter",
        "X-CSRF-Token: hunter2hunter",
        "api_key=hunter2hunter",
        "x-api-key: hunter2hunter",
        "client_secret=hunter2hunter",
        "sessionid=hunter2hunter",
        '{"password": "hunter2hunter"}',
        "{'token': 'hunter2hunter'}",
        'password="hunter2hunter"',
        "token: Bearer hunter2hunter",
    ],
)
def test_пары_ключ_значение_с_секретными_именами_вырезаются(line: str) -> None:
    assert "hunter2hunter" not in clean(f"ошибка входа: {line} (повтор)")


def test_значение_в_кавычках_с_пробелом_вырезается_целиком() -> None:
    assert clean("password='hunter two three' ok") == f"password='{MASK}' ok"
    assert clean('{"password": "hunter two", "x": 1}') == f'{{"password": "{MASK}", "x": 1}}'


def test_обычные_слова_с_корнем_token_и_secret_не_искажаются() -> None:
    for text in ("tokens: 5", "tokenizer: fast", "secretary: Ana", "max_tokens 100", "парольный"):
        assert clean(text) == text


# --- токен Telegram ---


def test_токен_telegram_в_адресе_запроса() -> None:
    out = clean(f"POST https://api.telegram.org/bot{TOKEN}/getMe -> 401")
    assert TOKEN not in out
    assert "123456789" not in out
    assert out.startswith("POST https://api.telegram.org/bot")
    assert out.endswith("/getMe -> 401")


@given(
    digits=st.integers(100_000, 999_999_999_999).map(str),
    tail=st.text(
        alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-",
        min_size=30,
        max_size=45,
    ),
    before=st.sampled_from(["", " ", "bot", "/bot", "token=", "x_", '"', "(", "id:"]),
    after=st.sampled_from(["", " ", ")", "/getMe", '"', "\n", ".", ","]),
)
def test_токен_telegram_любого_вида_вырезается_целиком(
    digits: str, tail: str, before: str, after: str
) -> None:
    """Хвост токена может оканчиваться на «-» или «_», перед ним — слово; остатка быть не должно."""
    token = f"{digits}:{tail}"
    out = clean(f"{before}{token}{after}")
    assert token not in out
    assert tail[-10:] not in out


# --- почта, телефоны, длинные номера ---


@pytest.mark.parametrize(
    "mail",
    ["ivan@example.com", "user.name+tag@mail.example.co.uy", "иван@пример.рф", "a_b-c@x-y.org"],
)
def test_адреса_почты_вырезаются(mail: str) -> None:
    out = clean(f"владелец {mail}, письмо ушло")
    assert mail not in out
    assert out == f"владелец {MASK}, письмо ушло" or out.startswith("владелец ")


@pytest.mark.parametrize(
    "phone",
    ["+59899123456", "+598 99 123 456", "+598-99-123-456", "+598 (99) 123-456", "+7 999 123-45-67"],
)
def test_телефоны_с_плюсом_вырезаются(phone: str) -> None:
    out = clean(f"звонить на {phone} вечером")
    assert out == f"звонить на {MASK} вечером"


@pytest.mark.parametrize("number", ["123456789", "1234567890", "123456789012", "98765432109876"])
@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("документ ", ""),
        ("документ ", "."),
        ("документ ", ","),
        ("документ ", ")"),
        ("doc-", ""),
        ("A", ""),
        ('"', '"'),
        ("id=", ";"),
        ("№", "\n"),
        ("", ""),
    ],
)
def test_длинные_номера_от_девяти_цифр_вырезаются_в_любом_окружении(
    number: str, before: str, after: str
) -> None:
    out = clean(f"{before}{number}{after}")
    assert number not in out
    assert MASK in out


def test_граница_длины_номера_восемь_цифр_остаются_девять_вырезаются() -> None:
    assert clean("номер 12345678") == "номер 12345678"
    assert clean("номер 123456789") == f"номер {MASK}"


def test_epoch_из_десяти_цифр_вырезается_осознанно() -> None:
    """Решение: десятизначные секунды epoch искажаются, но бот пишет время в ISO, а номер
    документа важнее такой метки; дробный epoch и миллисекунды-флоат остаются читаемыми."""
    assert clean("ts=1700000000") == f"ts={MASK}"
    assert clean("ts=1700000000.123") == "ts=1700000000.123"
    assert clean("took 0.123456789012 s") == "took 0.123456789012 s"


# --- ложные срабатывания ---


@pytest.mark.parametrize(
    "text",
    [
        "2027-02-23",
        "23.02.2027",
        "Найдено в 21:00:07, уведомление за 0,4 с.",
        "21:00:07.123456",
        "2026-11-12T15:00:00+00:00",
        "2027-02-23T21:00:07-03:00",
        "2026-11-12T15:00:00.123456Z",
        "2026-11-12 15:00:00,123 INFO rcbot.application.poll_loop: проверка прошла",
        "127.0.0.1",
        "127.0.0.1:8443",
        "https://127.0.0.1:50321/api?month=2027-02",
        "порт 8080, порт 443",
        "latency 312.5 мс, p95 1234.567 мс, 4300 проверок за сутки",
        "версия 3.14.0, aiohttp 3.14.5, uv 0.9.12",
        "poll_loop.py:451 перезапуск",
        "RCBOT_POLL_INTERVAL_SEC=20 RCBOT_JOURNAL_MAX_GB=10",
        "интервал 20 с (базовый 60 с), месяцев 6, входов за час: 2",
        "No hay cupos disponibles",
        "Oficina Tramite Extranjeros, Av. Uruguay 933",
        "• 23.02.2027 (вт): 10:30, 10:45, 11:00",
        "chat_id отброшен; sha256 a3f9c2d1e5b7048f",
    ],
)
def test_даты_время_порты_версии_и_адреса_не_искажаются(text: str) -> None:
    assert clean(text) == text


@given(st.text(alphabet="абвгдеёжз abcdef,.;:-()", max_size=60))
def test_текст_без_секретоподобного_не_меняется(text: str) -> None:
    assert clean(text) == text


# --- BUG-T6-1: шаблон почты не должен быть квадратичным ---


@pytest.mark.parametrize(
    "text",
    [
        "a" * 20_000,
        "a." * 10_000,
        "a-b" * 7_000,
        "a@" * 10_000,
        "a" * 20_000 + "@" + "b" * 20_000,
        "x" * 20_000 + "@",
    ],
    ids=["слово", "точки", "дефисы", "собаки", "слово-собака-слово", "слово-собака"],
)
def test_шаблон_почты_линеен_на_длинных_строках_без_пробелов(text: str) -> None:
    started = time.perf_counter()
    clean(text)
    assert time.perf_counter() - started < 0.5


def test_почта_после_длинного_слова_всё_равно_вырезается() -> None:
    out = clean("x" * 5_000 + " " + "ivan.petrov@example.com")
    assert out == "x" * 5_000 + f" {MASK}"
