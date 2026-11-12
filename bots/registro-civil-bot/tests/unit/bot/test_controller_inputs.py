"""Controller получает входы из Telegram: защита должна быть и в нём, а не только в обработчиках.

Находки вне зоны T5 (application/control.py, application/runtime.py) оформлены как xfail со ссылкой
BUG-T5-N; тимлид исправляет код и снимает метку.
"""

from __future__ import annotations

import contextlib
import math

import pytest

from rcbot.application.runtime import clamp_interval
from tests.unit.bot.harness import make_rig


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T5-1: clamp_interval(nan) возвращает nan — проверка «не меньше 10 с» "
    "пропускает NaN (application/runtime.py:30, control.py:set_interval)",
)
async def test_controller_не_принимает_nan_как_интервал() -> None:
    rig = await make_rig()
    with contextlib.suppress(ValueError):
        await rig.controller.set_interval(math.nan)
    assert math.isfinite(rig.runtime.base_interval_sec)
    assert rig.runtime.base_interval_sec >= 10.0


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T5-1: clamp_interval(nan) возвращает nan (application/runtime.py:30)",
)
def test_clamp_interval_nan_не_возвращает_nan() -> None:
    assert not math.isnan(clamp_interval(math.nan))


@pytest.mark.xfail(
    strict=True,
    reason="BUG-T5-2: Controller.pause(огромные минуты) падает OverflowError, "
    "а не отклоняется (application/control.py:pause)",
)
async def test_controller_pause_с_огромным_числом_минут_не_падает_overflow() -> None:
    rig = await make_rig()
    with contextlib.suppress(ValueError):  # осмысленный отказ допустим
        await rig.controller.pause(10**12)
    assert rig.runtime.paused_until is None or rig.runtime.paused_until > rig.clock.now()
