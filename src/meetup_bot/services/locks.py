"""Общий реестр блокировок встреч.

Любая операция, меняющая места встречи (запись, отмена, удаление участника, смена лимита,
продвижение из очереди, бан со снятием записей), берёт блокировку встречи отсюда. Процесс
один, поэтому блокировок в памяти достаточно (раздел 9.3). Экземпляр создаётся один раз при
сборке приложения и передаётся во все сервисы.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict


class EventLocks:
    def __init__(self) -> None:
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, event_id: int) -> asyncio.Lock:
        return self._locks[event_id]
