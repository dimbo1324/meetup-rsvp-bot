import pytest

from meetup_bot import __version__
from meetup_bot.config import Settings


def test_owner_ids_разбираются_из_строки() -> None:
    settings = Settings(
        _env_file=None,
        bot_token="123456:TEST-TOKEN",
        owner_ids="111, 222",  # type: ignore[arg-type]
        service_chat_id=-100123,
    )
    assert settings.owner_ids == (111, 222)
    assert settings.env == "prod"


def test_без_токена_ошибка(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ValueError):
        Settings(_env_file=None, owner_ids="1", service_chat_id=-1)  # type: ignore[arg-type,call-arg]


def _settings(**kwargs: object) -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:TEST-TOKEN",
        owner_ids="1",  # type: ignore[arg-type]
        service_chat_id=-1,
        **kwargs,  # type: ignore[arg-type]
    )


def test_версия_сборки_из_переменной_окружения(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_VERSION", "2026.10.25-abc")
    assert _settings().app_version == "2026.10.25-abc"


@pytest.mark.parametrize("value", ["", "  "])
def test_пустая_версия_сборки_заменяется_версией_пакета(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("APP_VERSION", value)
    assert _settings().app_version == __version__


def test_без_переменной_версия_пакета(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_VERSION", raising=False)
    assert _settings().app_version == __version__


@pytest.mark.parametrize(("env", "expected"), [("dev", True), ("prod", False)])
def test_признак_dev_режима(env: str, expected: bool) -> None:
    assert _settings(env=env).is_dev is expected
