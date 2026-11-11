"""Общий реестр блокировок встреч.

Любая операция, меняющая места встречи (запись, отмена, удаление участника, смена лимита,
продвижение из очереди, бан со снятием записей), берёт блокировку встречи отсюда. Процесс
один, поэтому блокировок в памяти достаточно (раздел 9.3). Экземпляр создаётся один раз при
сборке приложения и передаётся во все сервисы. Блокировка живёт, пока её кто-то держит или
ждёт: иначе словарь рос бы на каждую когда-либо созданную встречу.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Держатель плюс все ожидающие; пока не ноль, запись из словаря удалять нельзя
    users: int = 0


class EventLocks:
    def __init__(self) -> None:
        self._entries: dict[int, _Entry] = {}

    @asynccontextmanager
    async def lock(self, event_id: int) -> AsyncIterator[None]:
        entry = self._entries.setdefault(event_id, _Entry())
        # Счётчик меняется без await между проверкой и удалением, поэтому гонки нет:
        # новый ожидающий либо уже учтён, либо возьмёт свежую запись
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._entries.get(event_id) is entry:
                del self._entries[event_id]

    def __len__(self) -> int:
        return len(self._entries)
