"""Типы секретов не раскрывают значения: `SecretStr`, `Credentials`, `SessionCookie` (SPEC 2.6)."""

from __future__ import annotations

import copy
import json
import traceback

import keyring
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from rcbot.application.ports import SessionCookie
from rcbot.config.secrets import (
    DESCRIPTIONS,
    SERVICE,
    Credentials,
    SecretKey,
    SecretMissing,
    SecretStore,
)
from rcbot.infrastructure.logging.redaction import REDACTOR
from tests.privacy.helpers import Canaries

C = Canaries()


def _every_text_form(obj: object) -> list[str]:
    return [
        repr(obj),
        str(obj),
        f"{obj}",
        f"{obj!r}",
        "%s" % (obj,),  # noqa: UP031
        "%r" % (obj,),  # noqa: UP031
        format(obj),
        f"{obj}",
        repr([obj]),
        str({"k": obj}),
        str((obj,)),
        json.dumps(obj, default=str),
        str(ValueError(obj)),
        repr(ValueError(obj)),
        "".join(traceback.format_exception(ValueError(obj))),
    ]


def test_secretstr_во_всех_текстовых_видах_скрыт() -> None:
    secret = SecretStr(C.password)
    assert [t for t in _every_text_form(secret) if C.password in t] == []
    assert secret.get_secret_value() == C.password  # а явный запрос значения работает


def test_credentials_не_раскрывают_значения() -> None:
    creds = Credentials(SecretStr(C.doc_number), SecretStr(C.password))
    leaked = [t for t in _every_text_form(creds) if C.password in t or C.doc_number in t]
    assert leaked == []


def test_credentials_раскрытие_только_явным_вызовом() -> None:
    creds = Credentials(SecretStr(C.doc_number), SecretStr(C.password))
    assert creds.password.get_secret_value() == C.password
    assert copy.deepcopy(creds) == creds
    assert C.password not in repr(copy.deepcopy(creds))


def test_session_cookie_во_всех_текстовых_видах_скрыт() -> None:
    cookie = SessionCookie("simple_bpm_session", C.cookie_value, "bpmgob.mec.gub.uy")
    texts = _every_text_form(cookie) + _every_text_form([cookie, cookie])
    assert [t for t in texts if C.cookie_value in t] == []
    assert "simple_bpm_session" in repr(cookie)  # имя нужно для разбора, значение — нет
    assert "***" in repr(cookie)


@given(
    value=st.text(alphabet="0123456789abcdef", min_size=8, max_size=64),
    name=st.sampled_from(["simple_bpm_session", "TS0191f197", "target_bal"]),
)
def test_session_cookie_repr_не_содержит_значения_ни_для_каких_значений(
    value: str, name: str
) -> None:
    cookie = SessionCookie(name, value, "bpmgob.mec.gub.uy")
    assert value not in repr(cookie)
    assert value not in str([cookie])


def test_secret_store_отдаёт_secretstr_и_регистрирует_значение_в_фильтре() -> None:
    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.PASSWORD, C.password)
    assert REDACTOR.redact(C.password) == C.password  # до чтения фильтр секрета не знает
    value = store.get(SecretKey.PASSWORD)
    assert isinstance(value, SecretStr)
    assert C.password not in repr(value)
    assert REDACTOR.redact(f"пароль {C.password}!") == "пароль ***!"


@pytest.mark.parametrize("key", list(SecretKey))
def test_каждое_прочитанное_значение_регистрируется_get_и_require(key: SecretKey) -> None:
    store = SecretStore(on_read=REDACTOR.register)
    value = C.secrets()[key]
    store.set(key, value)
    REDACTOR.clear()
    store.require(key)
    assert value not in REDACTOR.redact(f"[{value}]"), key
    REDACTOR.clear()
    store.get(key)
    assert value not in REDACTOR.redact(f"[{value}]"), key


def test_credentials_регистрирует_номер_документа_и_пароль() -> None:
    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.DOC_NUMBER, C.doc_number)
    store.set(SecretKey.PASSWORD, C.password)
    store.credentials()
    assert REDACTOR.redact(f"{C.doc_number}/{C.password}") == "***/***"


def test_present_не_читает_значения_в_лог_и_не_вызывает_обратный_вызов() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    store.set(SecretKey.PASSWORD, C.password)
    assert store.present()[SecretKey.PASSWORD] is True
    assert seen == []


def test_пустое_и_отсутствующее_значение_не_вызывает_обратный_вызов() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    assert store.get(SecretKey.NTFY_TOPIC) is None
    keyring.set_password(SERVICE, SecretKey.NTFY_TOPIC.value, "")
    assert store.get(SecretKey.NTFY_TOPIC) is None
    assert seen == []


def test_исключение_об_отсутствии_секрета_не_содержит_значений_и_называет_действие() -> None:
    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.DOC_NUMBER, C.doc_number)
    with pytest.raises(SecretMissing) as info:
        store.require(SecretKey.PASSWORD)
    assert C.doc_number not in str(info.value)
    assert DESCRIPTIONS[SecretKey.PASSWORD] in str(info.value)
    assert "rcbot init" in str(info.value)


def test_set_убирает_пробелы_по_краям_как_и_регистрация() -> None:
    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.PASSWORD, f"  {C.password}\n")
    value = store.require(SecretKey.PASSWORD)
    assert value.get_secret_value() == C.password


def test_delete_убирает_секрет_из_хранилища() -> None:
    store = SecretStore()
    store.set(SecretKey.PASSWORD, C.password)
    assert store.delete(SecretKey.PASSWORD) is True
    assert store.get(SecretKey.PASSWORD) is None
    assert keyring.get_password(SERVICE, SecretKey.PASSWORD.value) is None
