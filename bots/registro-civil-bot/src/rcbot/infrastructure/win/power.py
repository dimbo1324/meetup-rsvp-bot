"""Защита от сна (SPEC 4.6): пока бот работает, Windows не уводит ноутбук в спящий режим.

`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` действует, пока жив поток, который
его вызвал; дисплей при этом гаснуть может. На других системах — ничего не делает.
"""

from __future__ import annotations

import logging
import sys
from types import TracebackType

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    def __init__(self) -> None:
        self.active = False

    def __enter__(self) -> KeepAwake:
        if sys.platform == "win32":
            import ctypes

            result = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )
            self.active = result != 0
            if not self.active:
                logger.warning("не удалось запретить сон: SetThreadExecutionState вернул 0")
        return self

    def __exit__(
        self, exc: type[BaseException] | None, value: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self.active and sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self.active = False
