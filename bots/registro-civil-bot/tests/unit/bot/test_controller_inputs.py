"""Controller получает входы из Telegram: защита должна быть и в нём, а не только в обработчиках.

Раньше здесь были ошибки BUG-T5-1 (NaN как интервал) и BUG-T5-2 (OverflowError в паузе): оба
исправлены при интеграции этапа 2, тесты стали обычными.
"""

from __future__ import annotations

import contextlib
import math

import pytest

from rcbot.application.runtime import clamp_interval
from tests.unit.bot.harness import make_rig


async def test_controller_не_принимает_nan_как_интервал() -> None:
    rig = await make_rig()
    with contextlib.suppress(ValueError):
        await rig.controller.set_interval(math.nan)
    assert math.isfinite(rig.runtime.base_interval_sec)
    assert rig.runtime.base_interval_sec >= 10.0


def test_clamp_interval_nan_отклоняется() -> None:
    with pytest.raises(ValueError):
        clamp_interval(math.nan)


async def test_controller_pause_с_огромным_числом_минут_не_падает_overflow() -> None:
    rig = await make_rig()
    with contextlib.suppress(ValueError):  # осмысленный отказ допустим
        await rig.controller.pause(10**12)
    assert rig.runtime.paused_until is None or rig.runtime.paused_until > rig.clock.now()
