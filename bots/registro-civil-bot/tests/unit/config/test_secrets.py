"""Секреты (SPEC 2.6, инварианты 6–7): только keyring, наружу — только SecretStr, всё в фильтр."""

from __future__ import annotations

import logging

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from rcbot.config.secrets import (
    DESCRIPTIONS,
    LEGACY_KEYS,
    REQUIRED,
    SERVICE,
    SecretKey,
    SecretMissing,
    SecretStore,
)
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR
from tests.conftest import MemoryKeyring

# Синтетические значения: ни одно не похоже на настоящий пароль или токен
DOC = "TEST-DOC-0001"
PASSWORD = "synthetic-pass-9f3k"
TOKEN = "TEST-TOKEN-abcdef"


def test_служба_keyring_называется_rcbot() -> None:
    assert SERVICE == "rcbot"


def test_у_каждого_ключа_есть_описание_а_обязательные_это_подмножество_ключей() -> None:
    assert set(DESCRIPTIONS) == set(SecretKey)
    assert set(REQUIRED) <= set(SecretKey)
    assert SecretKey.NTFY_TOPIC not in REQUIRED
    assert SecretKey.CALLMEBOT_USER not in REQUIRED


def test_set_и_get_кладут_значение_в_службу_rcbot(memory_keyring: MemoryKeyring) -> None:
    store = SecretStore()
    store.set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    assert memory_keyring.store == {("rcbot", "telegram_token"): PASSWORD}
    value = store.get(SecretKey.TELEGRAM_TOKEN)
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == PASSWORD


def test_set_обрезает_пробелы_и_переводы_строк(memory_keyring: MemoryKeyring) -> None:
    SecretStore().set(SecretKey.TELEGRAM_TOKEN, f"  {TOKEN}\r\n")
    assert memory_keyring.store[("rcbot", "telegram_token")] == TOKEN


def test_get_отсутствующего_секрета_это_none() -> None:
    assert SecretStore().get(SecretKey.CALLMEBOT_USER) is None


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_пустое_значение_это_нет_секрета(blank: str) -> None:
    store = SecretStore()
    store.set(SecretKey.NTFY_TOPIC, blank)
    assert store.get(SecretKey.NTFY_TOPIC) is None
    assert store.present()[SecretKey.NTFY_TOPIC] is False
    with pytest.raises(SecretMissing):
        store.require(SecretKey.NTFY_TOPIC)


def test_require_без_секрета_даёт_понятный_текст_без_значений() -> None:
    store = SecretStore()
    store.set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    with pytest.raises(SecretMissing) as info:
        store.require(SecretKey.CALLMEBOT_USER)
    message = str(info.value)
    assert DESCRIPTIONS[SecretKey.CALLMEBOT_USER] in message
    assert "rcbot init" in message
    assert PASSWORD not in message


@pytest.mark.parametrize("key", list(SecretKey))
def test_require_для_каждого_ключа_называет_именно_его(key: SecretKey) -> None:
    with pytest.raises(SecretMissing, match=DESCRIPTIONS[key].split(" (")[0]):
        SecretStore().require(key)


def test_require_возвращает_значение_когда_секрет_есть() -> None:
    store = SecretStore()
    store.set(SecretKey.TELEGRAM_CHAT_ID, "123456789")
    assert store.require(SecretKey.TELEGRAM_CHAT_ID).get_secret_value() == "123456789"


def test_обязательны_только_токен_и_владелец_номера_паспорта_и_пароля_нет() -> None:
    # Решение R13: бот не входит на gub.uy, секреты входа ему не нужны
    assert REQUIRED == (SecretKey.TELEGRAM_TOKEN, SecretKey.TELEGRAM_CHAT_ID)
    assert not {key.value for key in SecretKey} & set(LEGACY_KEYS)


def test_delete_legacy_удаляет_старые_секреты_входа_и_не_трогает_нужные(
    memory_keyring: MemoryKeyring,
) -> None:
    for name in LEGACY_KEYS:
        memory_keyring.set_password(SERVICE, name, "synthetic-old-value")
    store = SecretStore()
    store.set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    assert store.delete_legacy() == list(LEGACY_KEYS)
    assert memory_keyring.store == {(SERVICE, "telegram_token"): PASSWORD}
    assert store.delete_legacy() == []  # повтор — без ошибок


def test_delete_возвращает_был_ли_секрет() -> None:
    store = SecretStore()
    store.set(SecretKey.CALLMEBOT_USER, "@someone")
    assert store.delete(SecretKey.CALLMEBOT_USER) is True
    assert store.get(SecretKey.CALLMEBOT_USER) is None
    assert store.delete(SecretKey.CALLMEBOT_USER) is False


def test_present_показывает_заданность_всех_ключей() -> None:
    store = SecretStore()
    store.set(SecretKey.CALLMEBOT_USER, DOC)
    present = store.present()
    assert set(present) == set(SecretKey)
    assert present[SecretKey.CALLMEBOT_USER] is True
    assert [k for k, v in present.items() if not v and k in REQUIRED] == [
        SecretKey.TELEGRAM_TOKEN,
        SecretKey.TELEGRAM_CHAT_ID,
    ]


def test_present_не_читает_значения_в_фильтр() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    store.set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    store.present()
    assert seen == []


def test_backend_name_это_тестовое_хранилище_в_памяти() -> None:
    assert SecretStore().backend_name() == "MemoryKeyring"


# --- вырезание из логов ---


def test_on_read_регистрирует_каждое_прочитанное_значение() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    store.set(SecretKey.CALLMEBOT_USER, DOC)
    store.set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    store.get(SecretKey.CALLMEBOT_USER)
    store.require(SecretKey.TELEGRAM_TOKEN)
    assert seen == [DOC, PASSWORD]


def test_on_read_не_вызывается_для_отсутствующего_и_пустого() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    store.get(SecretKey.TELEGRAM_TOKEN)
    store.set(SecretKey.NTFY_TOPIC, "  ")
    store.get(SecretKey.NTFY_TOPIC)
    assert seen == []


def test_прочитанный_секрет_вырезается_из_логов_через_redactor() -> None:
    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    assert PASSWORD in f"вход с {PASSWORD}"
    store.get(SecretKey.TELEGRAM_TOKEN)
    assert REDACTOR.redact(f"вход с {PASSWORD} выполнен") == f"вход с {MASK} выполнен"


def test_до_чтения_значение_не_зарегистрировано_в_фильтре() -> None:
    SecretStore(on_read=REDACTOR.register).set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    assert PASSWORD in REDACTOR.redact(f"x {PASSWORD}")


def test_секрет_не_попадает_в_записанный_лог(caplog: pytest.LogCaptureFixture) -> None:
    from rcbot.infrastructure.logging.redaction import RedactionFilter

    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    secret = store.require(SecretKey.TELEGRAM_TOKEN)
    logger = logging.getLogger("rcbot.test.secrets")
    logger.addFilter(RedactionFilter())
    with caplog.at_level(logging.INFO):
        logger.info("пароль %s и значение %s", secret, secret.get_secret_value())
    assert PASSWORD not in caplog.text


# --- repr и str ---


def test_secretstr_не_раскрывает_значение_в_repr_str_и_f_строке() -> None:
    secret = SecretStr(PASSWORD)
    # Процентное форматирование — ровно то, что делает logging с аргументами записи
    percent_s, percent_r = "%s" % secret, "%r" % secret  # noqa: UP031
    for text in (repr(secret), str(secret), f"{secret}", percent_s, percent_r):
        assert PASSWORD not in text


def test_значение_из_хранилища_не_раскрывается_в_repr() -> None:
    store = SecretStore()
    store.set(SecretKey.TELEGRAM_TOKEN, PASSWORD)
    assert PASSWORD not in repr(store.get(SecretKey.TELEGRAM_TOKEN))
    assert PASSWORD not in repr(store)


@given(st.text(max_size=40))
def test_свойство_get_возвращает_обрезанное_значение_или_none(value: str) -> None:
    store = SecretStore()
    store.set(SecretKey.NTFY_TOPIC, value)
    got = store.get(SecretKey.NTFY_TOPIC)
    expected = value.strip()
    assert (got.get_secret_value() if got else None) == (expected or None)
