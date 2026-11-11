"""Данные пользователя Telegram: значение, которое передаётся между слоями (как `Clock`)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserInfo:
    """Данные пользователя Telegram, актуальные на момент взаимодействия."""

    id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
