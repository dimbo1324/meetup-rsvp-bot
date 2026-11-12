"""Секреты (SPEC 2.6, инварианты 6–7): только keyring, наружу — только SecretStr, всё в фильтр."""

from __future__ import annotations

import dataclasses
import logging

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from rcbot.config.secrets import (
    DESCRIPTIONS,
    REQUIRED,
    SERVICE,
    Credentials,
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
    store.set(SecretKey.PASSWORD, PASSWORD)
    assert memory_keyring.store == {("rcbot", "gubuy_password"): PASSWORD}
    value = store.get(SecretKey.PASSWORD)
    assert isinstance(value, SecretStr)
    assert value.get_secret_value() == PASSWORD


def test_set_обрезает_пробелы_и_переводы_строк(memory_keyring: MemoryKeyring) -> None:
    SecretStore().set(SecretKey.TELEGRAM_TOKEN, f"  {TOKEN}\r\n")
    assert memory_keyring.store[("rcbot", "telegram_token")] == TOKEN


def test_get_отсутствующего_секрета_это_none() -> None:
    assert SecretStore().get(SecretKey.DOC_NUMBER) is None


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
    store.set(SecretKey.PASSWORD, PASSWORD)
    with pytest.raises(SecretMissing) as info:
        store.require(SecretKey.DOC_NUMBER)
    message = str(info.value)
    assert DESCRIPTIONS[SecretKey.DOC_NUMBER] in message
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


def test_credentials_собирает_номер_документа_и_пароль() -> None:
    store = SecretStore()
    store.set(SecretKey.DOC_NUMBER, DOC)
    store.set(SecretKey.PASSWORD, PASSWORD)
    creds = store.credentials()
    assert creds.doc_number.get_secret_value() == DOC
    assert creds.password.get_secret_value() == PASSWORD


def test_credentials_без_пароля_падает_с_secret_missing() -> None:
    store = SecretStore()
    store.set(SecretKey.DOC_NUMBER, DOC)
    with pytest.raises(SecretMissing, match="пароль"):
        store.credentials()


def test_delete_возвращает_был_ли_секрет() -> None:
    store = SecretStore()
    store.set(SecretKey.CALLMEBOT_USER, "@someone")
    assert store.delete(SecretKey.CALLMEBOT_USER) is True
    assert store.get(SecretKey.CALLMEBOT_USER) is None
    assert store.delete(SecretKey.CALLMEBOT_USER) is False


def test_present_показывает_заданность_всех_ключей() -> None:
    store = SecretStore()
    store.set(SecretKey.DOC_NUMBER, DOC)
    present = store.present()
    assert set(present) == set(SecretKey)
    assert present[SecretKey.DOC_NUMBER] is True
    assert [k for k, v in present.items() if not v and k in REQUIRED] == [
        SecretKey.PASSWORD,
        SecretKey.TELEGRAM_TOKEN,
        SecretKey.TELEGRAM_CHAT_ID,
    ]


def test_present_не_читает_значения_в_фильтр() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    store.set(SecretKey.PASSWORD, PASSWORD)
    store.present()
    assert seen == []


def test_backend_name_это_тестовое_хранилище_в_памяти() -> None:
    assert SecretStore().backend_name() == "MemoryKeyring"


# --- вырезание из логов ---


def test_on_read_регистрирует_каждое_прочитанное_значение() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    store.set(SecretKey.DOC_NUMBER, DOC)
    store.set(SecretKey.PASSWORD, PASSWORD)
    store.get(SecretKey.DOC_NUMBER)
    store.require(SecretKey.PASSWORD)
    assert seen == [DOC, PASSWORD]
    store.credentials()
    assert seen[2:] == [DOC, PASSWORD]


def test_on_read_не_вызывается_для_отсутствующего_и_пустого() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    store.get(SecretKey.PASSWORD)
    store.set(SecretKey.NTFY_TOPIC, "  ")
    store.get(SecretKey.NTFY_TOPIC)
    assert seen == []


def test_прочитанный_секрет_вырезается_из_логов_через_redactor() -> None:
    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.PASSWORD, PASSWORD)
    assert PASSWORD in f"вход с {PASSWORD}"
    store.get(SecretKey.PASSWORD)
    assert REDACTOR.redact(f"вход с {PASSWORD} выполнен") == f"вход с {MASK} выполнен"


def test_до_чтения_значение_не_зарегистрировано_в_фильтре() -> None:
    SecretStore(on_read=REDACTOR.register).set(SecretKey.PASSWORD, PASSWORD)
    assert PASSWORD in REDACTOR.redact(f"x {PASSWORD}")


def test_секрет_не_попадает_в_записанный_лог(caplog: pytest.LogCaptureFixture) -> None:
    from rcbot.infrastructure.logging.redaction import RedactionFilter

    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.PASSWORD, PASSWORD)
    secret = store.require(SecretKey.PASSWORD)
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


def test_credentials_не_раскрывают_значения_в_repr_и_str() -> None:
    creds = Credentials(SecretStr(DOC), SecretStr(PASSWORD))
    for text in (repr(creds), str(creds), f"{creds}", repr([creds]), repr({"c": creds})):
        assert DOC not in text
        assert PASSWORD not in text


def test_значение_из_хранилища_не_раскрывается_в_repr() -> None:
    store = SecretStore()
    store.set(SecretKey.PASSWORD, PASSWORD)
    assert PASSWORD not in repr(store.get(SecretKey.PASSWORD))
    assert PASSWORD not in repr(store)


def test_credentials_неизменяемы() -> None:
    creds = Credentials(SecretStr(DOC), SecretStr(PASSWORD))
    with pytest.raises(dataclasses.FrozenInstanceError):
        creds.password = SecretStr("другой")  # type: ignore[misc]


@given(st.text(max_size=40))
def test_свойство_get_возвращает_обрезанное_значение_или_none(value: str) -> None:
    store = SecretStore()
    store.set(SecretKey.NTFY_TOPIC, value)
    got = store.get(SecretKey.NTFY_TOPIC)
    expected = value.strip()
    assert (got.get_secret_value() if got else None) == (expected or None)
