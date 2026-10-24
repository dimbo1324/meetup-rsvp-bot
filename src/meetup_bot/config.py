"""Конфигурация из переменных окружения (раздел 15.1 спецификации)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SecretStr: токен не попадает в repr, логи и трейсбеки настроек
    bot_token: SecretStr
    owner_ids: Annotated[tuple[int, ...], NoDecode] = Field(min_length=1)
    service_chat_id: int
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    env: Literal["dev", "prod"] = "prod"
    log_level: str = "INFO"
    sentry_dsn: SecretStr = SecretStr("")

    @field_validator("bot_token")
    @classmethod
    def _check_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        # Формат BotFather: <числовой id>:<строка>; ловим пустой или явно обрезанный токен
        bot_id, _, secret = token.partition(":")
        if not bot_id.isdigit() or len(secret) < 5:
            raise ValueError("ожидается токен от BotFather вида 123456:ABC…")
        return SecretStr(token)

    @field_validator("owner_ids", mode="before")
    @classmethod
    def _parse_owner_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(int(part) for part in value.split(",") if part.strip())
        return value


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
