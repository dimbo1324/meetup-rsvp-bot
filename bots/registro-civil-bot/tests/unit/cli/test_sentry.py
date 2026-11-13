"""Sentry: выключен по умолчанию, при включении — без личных данных (SPEC 2.6, инвариант 6)."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from rcbot.config.settings import Settings
from rcbot.infrastructure import sentry
from rcbot.infrastructure.logging.redaction import MASK, REDACTOR

DSN = "https://publickey@o0.ingest.sentry.invalid/1"


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Подставной sentry_sdk: настоящий не должен ни инициализироваться, ни ходить в сеть."""
    calls: list[dict[str, Any]] = []
    fake = ModuleType("sentry_sdk")
    fake.init = lambda **kwargs: calls.append(kwargs)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    return calls


def test_по_умолчанию_sentry_выключен() -> None:
    settings = Settings()
    assert settings.sentry_enabled is False
    assert settings.sentry_dsn == ""


@pytest.mark.parametrize(
    ("enabled", "dsn"),
    [(False, ""), (False, DSN), (True, "")],
    ids=["выкл-без-dsn", "выкл-с-dsn", "вкл-без-dsn"],
)
def test_без_флага_или_без_dsn_sdk_не_вызывается(
    sdk: list[dict[str, Any]], enabled: bool, dsn: str
) -> None:
    assert sentry.init_sentry(enabled, dsn, "rcbot@test") is False
    assert sdk == []


def test_включённый_sentry_настроен_без_личных_данных(sdk: list[dict[str, Any]]) -> None:
    assert sentry.init_sentry(True, DSN, "rcbot@1.2.3") is True

    [config] = sdk
    assert config["dsn"] == DSN
    assert config["release"] == "rcbot@1.2.3"
    assert config["send_default_pii"] is False
    assert config["include_local_variables"] is False
    assert config["max_request_body_size"] == "never"
    assert config["traces_sample_rate"] == 0.0
    assert config["before_send"] is sentry._before_send
    assert config["server_name"] == "rcbot"  # без имени компьютера


def test_before_send_вырезает_секреты_из_всего_события() -> None:
    REDACTOR.register("syn-password-Hh7k")
    event = {
        "message": "вход с паролем syn-password-Hh7k",
        "exception": {"values": [{"value": "Cookie: simple_bpm_session=abc123def"}]},
        "tags": ["ok", 5, None],
    }

    cleaned = sentry._before_send(event, {})

    assert cleaned is not None
    text = str(cleaned)
    assert "syn-password-Hh7k" not in text
    assert "abc123def" not in text
    assert MASK in cleaned["message"]
    assert cleaned["tags"] == ["ok", 5, None]  # безопасная часть события не портится


def test_before_send_не_ломается_на_нестроковых_значениях() -> None:
    from datetime import UTC, datetime

    cleaned = sentry._before_send({"when": datetime(2026, 11, 12, tzinfo=UTC), "n": 1}, {})
    assert cleaned is not None
    assert cleaned["n"] == 1
    assert "2026-11-12" in cleaned["when"]


def test_before_send_вырезает_секреты_со_спецсимволами_json() -> None:
    """Кавычка и обратная косая в пароле в тексте JSON экранируются: поиск по сырому тексту
    JSON такой пароль не находил бы, и он ушёл бы в Sentry."""
    password = r'pa"ss\word-Zz9'
    REDACTOR.register(password)
    event = {"message": f"неверный пароль {password}", "extra": {f"ключ {password}": password}}

    cleaned = sentry._before_send(event, {})

    assert cleaned is not None
    assert password not in str(cleaned)
    assert "Zz9" not in str(cleaned)
    assert cleaned["message"] == f"неверный пароль {MASK}"


def test_before_send_не_меняет_структуру_события() -> None:
    event = {"a": [1, 2.5, True, None, {"b": "текст"}], "c": {"d": []}}
    assert sentry._before_send(event, {}) == event


def test_before_send_отбрасывает_событие_которое_нельзя_безопасно_очистить() -> None:
    """Лучше потерять событие, чем отправить неочищенным: цикл в данных — отказ."""
    circular: dict[str, Any] = {}
    circular["self"] = circular
    assert sentry._before_send(circular, {}) is None


def test_before_send_длинное_число_в_событии_маскируется_как_в_журнале() -> None:
    chat_id = 123456789012
    cleaned = sentry._before_send({"extra": {"chat_id": chat_id, "n": 5, "x": 1.5}}, {})
    assert cleaned is not None
    assert cleaned["extra"] == {"chat_id": MASK, "n": 5, "x": 1.5}
    assert str(chat_id) not in str(cleaned)


def test_before_send_зарегистрированный_секрет_числом_вырезается() -> None:
    REDACTOR.register("98765432")
    cleaned = sentry._before_send({"extra": {"code": 98765432}}, {})
    assert cleaned is not None
    assert "98765432" not in str(cleaned)
