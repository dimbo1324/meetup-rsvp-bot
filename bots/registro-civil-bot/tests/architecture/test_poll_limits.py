"""Инварианты 3 и 4 (SPEC 0.3): интервал не ниже 10 с, не больше 6 входов в час, 60 с для DOM.

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
from rcbot.application.session_manager import HOUR, SessionManager
from rcbot.config.settings import (
    MAX_POLL_INTERVAL_SEC,
    MIN_DOM_POLL_INTERVAL_SEC,
    MIN_POLL_INTERVAL_SEC,
    Settings,
    load_settings,
)
from rcbot.domain.detector import Detector
from rcbot.domain.errors import ReloginBudgetExceeded, SessionExpired
from rcbot.domain.models import AvailabilitySnapshot, SourceKind, WatchTarget
from tests.fakes import (
    T0,
    TARGET,
    FakeAuthenticator,
    FakeClock,
    FakeSource,
    ManualClock,
    MemoryEventLog,
    MemoryIncidentRepository,
    MemoryKV,
    MemoryStatsRepository,
    settle,
)


def test_константы_пределов_зашиты_в_коде() -> None:
    assert MIN_POLL_INTERVAL_SEC == 10
    assert MIN_DOM_POLL_INTERVAL_SEC == 60
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


@pytest.mark.parametrize("value", [0, 7, 100])
def test_входов_в_час_в_настройках_не_больше_шести(value: int) -> None:
    with pytest.raises(ValueError, match=r"relogin_max_per_hour"):
        Settings(relogin_max_per_hour=value)


def test_шесть_входов_в_час_по_умолчанию_и_допустимо() -> None:
    assert Settings().relogin_max_per_hour == 6
    assert Settings(relogin_max_per_hour=1).relogin_max_per_hour == 1
    assert Settings(relogin_max_per_hour=6).relogin_max_per_hour == 6


# --- сборка цикла из фейков ---


class _LowRng(random.Random):
    """Худший разброс: всегда нижняя граница."""

    def uniform(self, a: float, b: float) -> float:
        return a


def make_loop(
    *,
    kind: SourceKind = SourceKind.API,
    base: float = 20.0,
    jitter: float = 0.1,
    source: FakeSource | None = None,
    auth: FakeAuthenticator | None = None,
    kv: MemoryKV | None = None,
    ladder_age: timedelta = timedelta(hours=40),
    clock: FakeClock | ManualClock | None = None,
) -> tuple[PollLoop, FakeClock | ManualClock, MemoryKV, SessionManager]:
    clock = clock or FakeClock()
    kv = kv or MemoryKV()
    runtime = RuntimeSettings(base, 6, T0 - ladder_age)
    session = SessionManager(auth or FakeAuthenticator(), clock, 6)
    loop = PollLoop(
        primary=source or FakeSource([frozenset()], kind=kind, clock=clock),
        fallback=None,
        session=session,
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
    return loop, clock, kv, session


def make_controller(loop: PollLoop, clock: FakeClock, session: SessionManager) -> Controller:
    book = IncidentBook(MemoryIncidentRepository(), clock, TARGET.id)
    alerts = AlertOrchestrator(
        {},
        {},
        MessageComposer(Settings().tz, "https://bpmgob.mec.gub.uy/x"),
        book,
        session,
        loop.runtime,
        clock,
        NoopCoordinator(),
    )
    return Controller(
        loop, alerts, book, MemoryIncidentRepository(), MemoryEventLog(), session, clock
    )


# --- Telegram, меню и хранилище ---


@pytest.mark.parametrize("value", [9.99, 5.0, 1.0, 0.0, -10.0])
async def test_loop_set_interval_не_опускается_ниже_десяти_и_в_базу_идёт_ограниченное(
    value: float,
) -> None:
    loop, _, kv, _ = make_loop()
    assert await loop.set_interval(value) == MIN_POLL_INTERVAL_SEC
    assert loop.runtime.base_interval_sec == MIN_POLL_INTERVAL_SEC
    assert float(kv.data[K_INTERVAL]) >= MIN_POLL_INTERVAL_SEC


@pytest.mark.parametrize("value", [9.99, 5.0, 0.5, 0.0, -1.0])
async def test_controller_set_interval_отказывает_и_ничего_не_меняет(value: float) -> None:
    loop, clock, kv, session = make_loop(base=20.0)
    controller = make_controller(loop, clock, session)
    with pytest.raises(IntervalTooSmall):
        await controller.set_interval(value)
    assert loop.runtime.base_interval_sec == 20.0
    assert K_INTERVAL not in kv.data


async def test_controller_принимает_ровно_десять() -> None:
    loop, clock, _, session = make_loop(base=20.0)
    assert await make_controller(loop, clock, session).set_interval(10.0) == 10.0


async def test_controller_отклоняет_nan() -> None:
    loop, clock, _, session = make_loop()
    with pytest.raises(IntervalTooSmall):
        await make_controller(loop, clock, session).set_interval(float("nan"))


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


# --- лестница, режимы, разброс ---


@given(
    base=st.floats(10, 3600),
    elapsed_hours=st.floats(0, 120),
    kind=st.sampled_from([SourceKind.API, SourceKind.DOM]),
    back=st.integers(0, 3),
)
def test_лестница_и_шаги_назад_никогда_не_дают_меньше_предела(
    base: float, elapsed_hours: float, kind: SourceKind, back: int
) -> None:
    loop, _, _, _ = make_loop(kind=kind, base=base)
    now = T0 + timedelta(hours=elapsed_hours)
    loop.runtime.ladder_started_at = T0
    for _ in range(back):
        loop.runtime.step_back(now)
    interval = loop.current_interval(now)
    assert interval >= MIN_POLL_INTERVAL_SEC
    if kind is SourceKind.DOM:
        assert interval >= MIN_DOM_POLL_INTERVAL_SEC


@given(base=st.floats(10, 120), jitter=st.floats(0, 0.5))
def test_разброс_не_опускает_расписание_ниже_десяти_секунд(base: float, jitter: float) -> None:
    loop, clock, _, _ = make_loop(base=base, jitter=jitter)
    loop._next_due = clock.monotonic()
    assert loop._schedule_next() >= MIN_POLL_INTERVAL_SEC - 1e-9


def test_режим_dom_не_чаще_раза_в_минуту_без_разброса() -> None:
    loop, clock, _, _ = make_loop(kind=SourceKind.DOM, base=10.0, jitter=0.0)
    loop._next_due = clock.monotonic()
    assert loop._schedule_next() >= MIN_DOM_POLL_INTERVAL_SEC


@pytest.mark.parametrize("jitter", [0.1, 0.5])
def test_режим_dom_не_чаще_раза_в_минуту_и_с_разбросом(jitter: float) -> None:
    loop, clock, _, _ = make_loop(kind=SourceKind.DOM, base=20.0, jitter=jitter)
    loop._next_due = clock.monotonic()
    assert loop._schedule_next() >= MIN_DOM_POLL_INTERVAL_SEC


@pytest.mark.parametrize(("kind", "minimum"), [(SourceKind.API, 10.0), (SourceKind.DOM, 60.0)])
async def test_после_входа_проверки_идут_не_чаще_минимума_источника(
    kind: SourceKind, minimum: float
) -> None:
    """Сайт всё время отвечает «сессия истекла», вход мгновенный: запросы не должны идти подряд."""
    clock = ManualClock()
    stamps: list[float] = []

    class Expired(FakeSource):
        async def fetch(self, target: WatchTarget) -> AvailabilitySnapshot:
            stamps.append(clock.monotonic())
            raise SessionExpired("страница входа")

    loop, _, _, _ = make_loop(kind=kind, source=Expired(kind=kind), clock=clock)
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
    assert min(gaps) >= minimum - 1e-6


# --- не больше 6 входов в час ---


async def test_седьмой_вход_за_час_отклоняется_и_не_вызывает_вход() -> None:
    clock = FakeClock()
    auth = FakeAuthenticator()
    session = SessionManager(auth, clock, 6)
    for _ in range(6):
        await session.relogin("тест", force=True)
        clock.advance(60)
    with pytest.raises(ReloginBudgetExceeded):
        await session.relogin("тест", force=True)
    assert auth.logins == 6


async def test_неудачные_входы_тоже_расходуют_лимит() -> None:
    from rcbot.domain.errors import LoginFailed

    clock = FakeClock()
    auth = FakeAuthenticator(failures=[LoginFailed("нет")] * 6)
    session = SessionManager(auth, clock, 6)
    for _ in range(6):
        with pytest.raises(LoginFailed):
            await session.relogin("тест")
    with pytest.raises(ReloginBudgetExceeded):
        await session.relogin("тест")
    assert auth.logins == 6


async def test_лимит_освобождается_через_час_и_принудительный_вход_его_не_обходит() -> None:
    clock = FakeClock()
    auth = FakeAuthenticator()
    session = SessionManager(auth, clock, 6)
    for _ in range(6):
        await session.relogin("тест", force=True)
    with pytest.raises(ReloginBudgetExceeded):
        await session.relogin("по команде владельца", force=True)
    clock.advance(HOUR - 1)
    with pytest.raises(ReloginBudgetExceeded):
        await session.relogin("тест", force=True)
    clock.advance(2)
    await session.relogin("тест", force=True)
    assert auth.logins == 7


@given(gaps=st.lists(st.floats(0, 1500), min_size=1, max_size=40), limit=st.integers(1, 6))
async def test_в_любом_окне_в_час_не_больше_лимита_входов(gaps: list[float], limit: int) -> None:
    clock = FakeClock()
    auth = FakeAuthenticator()
    session = SessionManager(auth, clock, limit)
    stamps: list[float] = []
    for gap in gaps:
        clock.advance(gap)
        try:
            await session.relogin("тест", force=True)
        except ReloginBudgetExceeded:
            continue
        stamps.append(clock.monotonic())
    for start in stamps:
        assert sum(1 for s in stamps if start <= s < start + HOUR) <= limit
    await settle(1)
