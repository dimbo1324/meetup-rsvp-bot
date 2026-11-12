"""Защита от сна (SPEC 4.6): SetThreadExecutionState на время работы, снимается при выходе."""

from __future__ import annotations

import ctypes
import logging
import sys
from types import SimpleNamespace

import pytest

from rcbot.infrastructure.win.power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake


class FakeKernel:
    def __init__(self, result: int = 0x80000000) -> None:
        self.result = result
        self.calls: list[int] = []

    def SetThreadExecutionState(self, flags: int) -> int:
        self.calls.append(flags)
        return self.result


@pytest.fixture
def kernel(monkeypatch: pytest.MonkeyPatch) -> FakeKernel:
    fake = FakeKernel()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=fake), raising=False)
    return fake


def test_флаги_соответствуют_документации_win32() -> None:
    assert ES_CONTINUOUS == 0x80000000
    assert ES_SYSTEM_REQUIRED == 0x00000001


def test_при_входе_запрещает_сон_при_выходе_возвращает_обычный_режим(kernel: FakeKernel) -> None:
    with KeepAwake() as awake:
        assert awake.active is True
        assert kernel.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    assert kernel.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]
    assert awake.active is False


def test_дисплею_гаснуть_не_запрещено(kernel: FakeKernel) -> None:
    with KeepAwake():
        pass
    assert all(not call & 0x00000002 for call in kernel.calls)  # ES_DISPLAY_REQUIRED


def test_сон_снова_разрешается_если_в_работе_было_исключение(kernel: FakeKernel) -> None:
    with pytest.raises(RuntimeError), KeepAwake():
        raise RuntimeError("сбой")
    assert kernel.calls[-1] == ES_CONTINUOUS


def test_отказ_системы_даёт_предупреждение_и_выход_ничего_не_вызывает(
    kernel: FakeKernel, caplog: pytest.LogCaptureFixture
) -> None:
    kernel.result = 0
    with caplog.at_level(logging.WARNING, logger="rcbot.infrastructure.win.power"):
        with KeepAwake() as awake:
            assert awake.active is False
        assert any("не удалось запретить сон" in r.getMessage() for r in caplog.records)
    assert kernel.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]


def test_повторное_использование_объекта(kernel: FakeKernel) -> None:
    awake = KeepAwake()
    with awake:
        pass
    with awake:
        pass
    assert kernel.calls == [
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED,
        ES_CONTINUOUS,
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED,
        ES_CONTINUOUS,
    ]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_вне_windows_ничего_не_делает(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    class Forbidden:
        def __getattr__(self, name: str) -> None:
            raise AssertionError("ctypes.windll вне Windows")

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(ctypes, "windll", Forbidden(), raising=False)
    with KeepAwake() as awake:
        assert awake.active is False
    assert awake.active is False
