"""Типы секретов не раскрывают значения: `SecretStr` и хранилище секретов (SPEC 2.6)."""

from __future__ import annotations

import json
import traceback

import keyring
import pytest
from pydantic import SecretStr

from rcbot.config.secrets import (
    DESCRIPTIONS,
    SERVICE,
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
    secret = SecretStr(C.telegram_token)
    assert [t for t in _every_text_form(secret) if C.telegram_token in t] == []
    assert secret.get_secret_value() == C.telegram_token  # а явный запрос значения работает


def test_secret_store_отдаёт_secretstr_и_регистрирует_значение_в_фильтре() -> None:
    store = SecretStore(on_read=REDACTOR.register)
    # Имя для звонка, а не токен: токен Telegram фильтр узнаёт по виду и без регистрации
    store.set(SecretKey.CALLMEBOT_USER, C.call_user)
    assert REDACTOR.redact(C.call_user) == C.call_user  # до чтения фильтр секрета не знает
    value = store.get(SecretKey.CALLMEBOT_USER)
    assert isinstance(value, SecretStr)
    assert C.call_user not in repr(value)
    assert REDACTOR.redact(f"звонок {C.call_user}!") == "звонок ***!"


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


def test_present_не_читает_значения_в_лог_и_не_вызывает_обратный_вызов() -> None:
    seen: list[str] = []
    store = SecretStore(on_read=seen.append)
    store.set(SecretKey.TELEGRAM_TOKEN, C.telegram_token)
    assert store.present()[SecretKey.TELEGRAM_TOKEN] is True
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
    store.set(SecretKey.CALLMEBOT_USER, C.call_user)
    with pytest.raises(SecretMissing) as info:
        store.require(SecretKey.TELEGRAM_TOKEN)
    assert C.call_user not in str(info.value)
    assert DESCRIPTIONS[SecretKey.TELEGRAM_TOKEN] in str(info.value)
    assert "rcbot init" in str(info.value)


def test_set_убирает_пробелы_по_краям_как_и_регистрация() -> None:
    store = SecretStore(on_read=REDACTOR.register)
    store.set(SecretKey.TELEGRAM_TOKEN, f"  {C.telegram_token}\n")
    value = store.require(SecretKey.TELEGRAM_TOKEN)
    assert value.get_secret_value() == C.telegram_token


def test_delete_убирает_секрет_из_хранилища() -> None:
    store = SecretStore()
    store.set(SecretKey.TELEGRAM_TOKEN, C.telegram_token)
    assert store.delete(SecretKey.TELEGRAM_TOKEN) is True
    assert store.get(SecretKey.TELEGRAM_TOKEN) is None
    assert keyring.get_password(SERVICE, SecretKey.TELEGRAM_TOKEN.value) is None
