"""Секреты — только в Windows Credential Manager через keyring (служба `rcbot`, SPEC 2.6).

На диск, в логи и в БД значения не попадают: наружу они выходят только как `SecretStr`, а
каждое прочитанное значение регистрируется в фильтре логов для вырезания.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

import keyring
import keyring.errors
from pydantic import SecretStr

SERVICE = "rcbot"


class SecretKey(StrEnum):
    TELEGRAM_TOKEN = "telegram_token"
    TELEGRAM_CHAT_ID = "telegram_chat_id"
    NTFY_TOPIC = "ntfy_topic"
    CALLMEBOT_USER = "callmebot_user"


REQUIRED = (SecretKey.TELEGRAM_TOKEN, SecretKey.TELEGRAM_CHAT_ID)

# Секреты входа на bpmgob (до решения R13): бот их больше не читает. `init` и `uninstall` удаляют
# их из Credential Manager, чтобы номер паспорта и пароль не лежали там без надобности
LEGACY_KEYS = ("doc_number", "gubuy_password")

DESCRIPTIONS: dict[SecretKey, str] = {
    SecretKey.TELEGRAM_TOKEN: "токен Telegram-бота от @BotFather",
    SecretKey.TELEGRAM_CHAT_ID: "chat_id владельца в Telegram",
    SecretKey.NTFY_TOPIC: "имя темы ntfy (случайное, 32+ символа)",
    SecretKey.CALLMEBOT_USER: "@username в Telegram для звонков CallMeBot",
}


class SecretMissing(Exception):
    """Нужного секрета нет в хранилище: запустите `rcbot init`."""


class SecretStore:
    def __init__(self, on_read: Callable[[str], None] | None = None) -> None:
        self._on_read = on_read

    def get(self, key: SecretKey) -> SecretStr | None:
        value = keyring.get_password(SERVICE, key.value)
        if not value:
            return None
        if self._on_read is not None:
            self._on_read(value)
        return SecretStr(value)

    def require(self, key: SecretKey) -> SecretStr:
        value = self.get(key)
        if value is None:
            raise SecretMissing(f"нет секрета «{DESCRIPTIONS[key]}»: выполните `rcbot init`")
        return value

    def set(self, key: SecretKey, value: str) -> None:
        keyring.set_password(SERVICE, key.value, value.strip())

    def delete(self, key: SecretKey) -> bool:
        try:
            keyring.delete_password(SERVICE, key.value)
        except keyring.errors.PasswordDeleteError:
            return False
        return True

    def present(self) -> dict[SecretKey, bool]:
        """Какие секреты заданы — без чтения значений в логи."""
        return {key: bool(keyring.get_password(SERVICE, key.value)) for key in SecretKey}

    def delete_legacy(self) -> list[str]:
        """Удаляет секреты прежнего входа на bpmgob; возвращает имена удалённых."""
        removed = []
        for name in LEGACY_KEYS:
            try:
                keyring.delete_password(SERVICE, name)
            except keyring.errors.PasswordDeleteError:
                continue
            removed.append(name)
        return removed

    def backend_name(self) -> str:
        return type(keyring.get_keyring()).__name__
