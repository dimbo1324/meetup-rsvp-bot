"""Интерфейс отметки явки в личке админа (раздел 5.8, сценарий 4.6).

ЗАГЛУШКА С ИТОГОВОЙ СИГНАТУРОЙ: реализацию пишет агент H5. Остальные модули (карточка
встречи, уведомитель) обращаются только к `AttendanceUI.send_sheet`.
"""

from __future__ import annotations

import enum


class SheetStatus(enum.StrEnum):
    SENT = "sent"
    NOT_STARTED = "not_started"
    WINDOW_CLOSED = "window_closed"
    CANCELLED = "cancelled"
    NO_ACCESS = "no_access"
    FAILED = "failed"


class AttendanceUI:
    async def send_sheet(self, admin_id: int, event_id: int) -> SheetStatus:
        """Отправляет админу список участников с переключателями ✅/🚫.

        Режим определяется по состоянию встречи: явка не отмечена — обычная отметка с
        кнопкой «Готово»; уже отмечена и окно 7 суток открыто — режим исправления.
        """
        return SheetStatus.FAILED
