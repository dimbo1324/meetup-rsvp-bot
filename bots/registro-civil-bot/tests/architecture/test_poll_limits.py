"""Инвариант 3 (SPEC 0.3): интервал опроса не ниже 10 с; на сайт бот не входит (R13).

Нижние границы зашиты в коде; здесь проверяется, что их нельзя обойти ни одним из путей
(`.env`, Telegram, меню, повреждённая БД, разброс расписания, лестница частоты).
"""

from __future__ import annotations

import asyncio
import itertools
import random
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rcbot.application.alerting import AlertOrchestrator
from rcbot.application.bus import EventBus
from rcbot.application.control import Controller, IntervalTooSmall
from rcbot.application.incidents import IncidentBook
from rcbot.application.messages import MessageComposer
from rcbot.application.poll_loop import PollLoop
from rcbot.application.ports import NoopCoordinator
from rcbot.application.runtime import (
    K_CHANNELS_OFF,
    K_INTERVAL,
    K_LADDER_START,
    K_MONTHS,
    K_PAUSED_UNTIL,
    RuntimeSettings,
    RuntimeStore,
    clamp_interval,
)
from rcbot.config.settings import (
    MAX_POLL_INTERVAL_SEC,
    MIN_POLL_INTERVAL_SEC,
    Settings,
    load_settings,
)
from rcbot.domain.detector import Detector
from rcbot.domain.errors import SiteUnavailable
from rcbot.domain.models import AvailabilitySnapshot, SourceKind, WatchTarget
from tests.fakes import (
    T0,
    TARGET,
    FakeClock,
    FakeSource,
    ManualClock,
    MemoryEventLog,
    MemoryIncidentRepository,
    MemoryKV,
    MemoryStatsRepository,
)


def test_константы_пределов_зашиты_в_коде() -> None:
    assert MIN_POLL_INTERVAL_SEC == 10
    assert MAX_POLL_INTERVAL_SEC == 3600


# --- настройки ---

TOO_SMALL = [9.99, 9, 5, 1, 0.001, 0, -1, -20]


@pytest.mark.parametrize("value", TOO_SMALL)
def test_settings_отклоняет_интервал_меньше_десяти_секунд_из_окружения(
    value: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RCBOT_POLL_INTERVAL_SEC", str(value))
    with pytest.raises(ValueError, match="интервал опроса"):
        Settings()


@pytest.mark.parametrize("value", TOO_SMALL)
def test_settings_отклоняет_интервал_меньше_десяти_секунд_из_аргумента_и_env_файла(
    value: float, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="интервал опроса"):
        Settings(poll_interval_sec=value)
    env = tmp_path / ".env"
    env.write_text(f"RCBOT_POLL_INTERVAL_SEC={value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="интервал опроса"):
        load_settings(env)


def test_settings_принимает_ровно_десять_и_ограничивает_сверху(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings(poll_interval_sec=10).poll_interval_sec == 10
    assert Settings(poll_interval_sec=10.0001).poll_interval_sec == 10.0001
    assert Settings(poll_interval_sec=10**9).poll_interval_sec == MAX_POLL_INTERVAL_SEC
    monkeypatch.setenv("RCBOT_POLL_INTERVAL_SEC", "20")
    assert Settings().poll_interval_sec == 20


def test_settings_отклоняет_nan_и_бесконечность(monkeypatch: pytest.MonkeyPatch) -> None:
    for text in ("nan", "-inf"):
        monkeypatch.setenv("RCBOT_POLL_INTERVAL_SEC", text)
        with pytest.raises(ValueError, match=r"."):
            Settings()


# --- сборка цикла из фейков ---


class _LowRng(random.Random):
    """Худший разброс: всегда нижняя граница."""

    def uniform(self, a: float, b: float) -> float:
        return a


def make_loop(
    *,
    kind: SourceKind = SourceKind.SAE,
    base: float = 20.0,
    jitter: float = 0.1,
    source: FakeSource | None = None,
    kv: MemoryKV | None = None,
    ladder_age: timedelta = timedelta(hours=40),
    clock: FakeClock | ManualClock | None = None,
) -> tuple[PollLoop, FakeClock | ManualClock, MemoryKV]:
    clock = clock or FakeClock()
    kv = kv or MemoryKV()
    runtime = RuntimeSettings(base, 6, T0 - ladder_age)
    loop = PollLoop(
        source=source or FakeSource([frozenset()], kind=kind, clock=clock),
        detector=Detector(),
        bus=EventBus(),
        clock=clock,
        runtime=runtime,
        runtime_store=RuntimeStore(kv),
        stats=MemoryStatsRepository(),
        event_log=MemoryEventLog(),
        target=TARGET,
        jitter=jitter,
        rng=_LowRng(),
    )
    return loop, clock, kv


def make_controller(loop: PollLoop, clock: FakeClock) -> Controller:
    book = IncidentBook(MemoryIncidentRepository(), clock, TARGET.id)
    alerts = AlertOrchestrator(
        {},
        {},
        MessageComposer(Settings().tz, "https://bpmgob.mec.gub.uy/x"),
        book,
        loop.runtime,
        clock,
        NoopCoordinator(),
    )
    return Controller(loop, alerts, book, MemoryIncidentRepository(), MemoryEventLog(), clock)


# --- Telegram, меню и хранилище ---


@pytest.mark.parametrize("value", [9.99, 5.0, 1.0, 0.0, -10.0])
async def test_loop_set_interval_не_опускается_ниже_десяти_и_в_базу_идёт_ограниченное(
    value: float,
) -> None:
    loop, _, kv = make_loop()
    assert await loop.set_interval(value) == MIN_POLL_INTERVAL_SEC
    assert loop.runtime.base_interval_sec == MIN_POLL_INTERVAL_SEC
    assert float(kv.data[K_INTERVAL]) >= MIN_POLL_INTERVAL_SEC


@pytest.mark.parametrize("value", [9.99, 5.0, 0.5, 0.0, -1.0])
async def test_controller_set_interval_отказывает_и_ничего_не_меняет(value: float) -> None:
    loop, clock, kv = make_loop(base=20.0)
    controller = make_controller(loop, clock)
    with pytest.raises(IntervalTooSmall):
        await controller.set_interval(value)
    assert loop.runtime.base_interval_sec == 20.0
    assert K_INTERVAL not in kv.data


async def test_controller_принимает_ровно_десять() -> None:
    loop, clock, _ = make_loop(base=20.0)
    assert await make_controller(loop, clock).set_interval(10.0) == 10.0


async def test_controller_отклоняет_nan() -> None:
    loop, clock, _ = make_loop()
    with pytest.raises(IntervalTooSmall):
        await make_controller(loop, clock).set_interval(float("nan"))


@pytest.mark.parametrize("stored", ["0", "-5", "1e-9", "3", "9.99", "-inf"])
async def test_повреждённое_малое_значение_в_kv_ограничивается_десятью(stored: str) -> None:
    kv = MemoryKV({K_INTERVAL: stored, K_LADDER_START: T0.isoformat()})
    settings = await RuntimeStore(kv).load(20.0, 6, T0)
    assert settings.base_interval_sec == MIN_POLL_INTERVAL_SEC


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (K_INTERVAL, "abc"),
        (K_INTERVAL, ""),
        (K_INTERVAL, "nan"),
        (K_MONTHS, "много"),
        (K_LADDER_START, "вчера"),
        (K_PAUSED_UNTIL, "завтра"),
        (K_CHANNELS_OFF, "telegram,fax"),
    ],
)
async def test_мусор_в_kv_не_ломает_запуск_и_не_снижает_интервал(key: str, value: str) -> None:
    kv = MemoryKV({K_LADDER_START: T0.isoformat(), key: value})
    settings = await RuntimeStore(kv).load(20.0, 6, T0)
    assert MIN_POLL_INTERVAL_SEC <= settings.base_interval_sec <= MAX_POLL_INTERVAL_SEC


@given(seconds=st.floats(allow_nan=False, allow_infinity=True))
def test_clamp_interval_всегда_в_пределах(seconds: float) -> None:
    assert MIN_POLL_INTERVAL_SEC <= clamp_interval(seconds) <= MAX_POLL_INTERVAL_SEC


# --- лестница и разброс ---


@given(base=st.floats(10, 3600), elapsed_hours=st.floats(0, 120), back=st.integers(0, 3))
def test_лестница_и_шаги_назад_никогда_не_дают_меньше_предела(
    base: float, elapsed_hours: float, back: int
) -> None:
    loop, _, _ = make_loop(base=base)
    now = T0 + timedelta(hours=elapsed_hours)
    loop.runtime.ladder_started_at = T0
    for _ in range(back):
        loop.runtime.step_back(now)
    assert loop.current_interval(now) >= MIN_POLL_INTERVAL_SEC


@given(base=st.floats(10, 120), jitter=st.floats(0, 0.5))
def test_разброс_не_опускает_расписание_ниже_десяти_секунд(base: float, jitter: float) -> None:
    loop, clock, _ = make_loop(base=base, jitter=jitter)
    loop._next_due = clock.monotonic()
    assert loop._schedule_next() >= MIN_POLL_INTERVAL_SEC - 1e-9


async def test_при_постоянных_сбоях_проверки_идут_не_чаще_минимума() -> None:
    """Сайт всё время недоступен: запросы не должны идти подряд, только реже и реже."""
    clock = ManualClock()
    stamps: list[float] = []

    class Down(FakeSource):
        async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
            stamps.append(clock.monotonic())
            raise SiteUnavailable("503")

    loop, _, _ = make_loop(source=Down(), clock=clock)
    task = asyncio.create_task(loop.run())
    try:
        for _ in range(60):
            await clock.advance(20)
    finally:
        loop.stop()
        await clock.advance(1000)
        await asyncio.wait_for(task, 5)
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert len(stamps) >= 3
    assert min(gaps) >= MIN_POLL_INTERVAL_SEC - 1e-6
    assert gaps == sorted(gaps)  # пауза растёт, а не повторяется «в лоб»
