import pytest

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
