"""Порты: то, что сервисам нужно от внешнего мира, без привязки к Telegram.

Реализации появятся в Telegram-слое (веха M2). Сервисы получают порты через конструктор;
по умолчанию подставляются «пустые» реализации, которые ничего не делают.
"""

from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    async def send_dm(self, user_id: int, text: str) -> bool:
        """Личное сообщение; False, если написать нельзя (нет диалога или бот заблокирован)."""
        ...

    async def send_service(self, text: str) -> None:
        """Сообщение в служебный чат админов."""
        ...

    async def send_attendance_prompt(self, admin_id: int, event_id: int) -> bool:
        """Список для отметки явки конкретному админу (формирует Telegram-слой)."""
        ...


class PostRefresher(Protocol):
    def schedule(self, event_id: int) -> None:
        """Поставить в очередь пересборку всех постов встречи (debounce на стороне реализации)."""
        ...


class NullNotifier:
    async def send_dm(self, user_id: int, text: str) -> bool:
        return False

    async def send_service(self, text: str) -> None:
        return None

    async def send_attendance_prompt(self, admin_id: int, event_id: int) -> bool:
        return False


class NullPostRefresher:
    def schedule(self, event_id: int) -> None:
        return None
