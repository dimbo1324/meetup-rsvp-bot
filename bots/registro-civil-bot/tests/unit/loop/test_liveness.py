"""LivenessMonitor (решение R6): «жив / завис» по ходу цикла опроса, восстановление, status.json."""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from tests.fakes import T0, FakeClock, ManualClock, RecordingJournal, settle
from tests.unit.loop.harness import Rig, make_rig

from rcbot import __version__
from rcbot.application.liveness import (
    HUNG_CHECK_SEC,
    HUNG_LATE_SEC,
    HUNG_MARGIN_SEC,
    HUNG_RETRY_SEC,
    STATUS_EVERY_SEC,
    LivenessMonitor,
)
from rcbot.application.poll_loop import FETCH_TIMEOUT_SEC, LOGIN_TIMEOUT_SEC
from rcbot.application.supervisor import Supervisor
from rcbot.domain.errors import ManualStepRequired, SiteUnavailable

EMPTY: frozenset = frozenset()  # type: ignore[type-arg]


class StubLoop:
    """Ход цикла опроса, которым управляет тест (то, что читает монитор)."""

    def __init__(self) -> None:
        self.tick_started_at: float | None = None
        self.next_due = 0.0

    def snapshot(self) -> dict[str, object]:
        return {"state": "running", "checks_total": 7}


class Harness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.loop = StubLoop()
        self.journal = RecordingJournal()
        self.notified: list[str] = []
        self.restarts = 0
        self.notify_error: Exception | None = None
        self.restart_error: Exception | None = None
        self.monitor = LivenessMonitor(
            self.loop,  # type: ignore[arg-type]
            self.journal,
            self.clock,
            self._notify,
            self._restart,
        )

    async def _notify(self, text: str) -> None:
        self.notified.append(text)
        if self.notify_error:
            raise self.notify_error

    async def _restart(self) -> None:
        self.restarts += 1
        if self.restart_error:
            raise self.restart_error

    def hang_check(self, seconds: float) -> None:
        self.loop.tick_started_at = self.clock.monotonic() - seconds

    def heal(self) -> None:
        self.loop.tick_started_at = None
        self.loop.next_due = self.clock.monotonic() + 20


# --- diagnose ---


def test_пороги_заданы_от_таймаутов_проверки_и_входа() -> None:
    # Проверка включает повторный вход: порог выше запроса и входа вместе взятых (COL-7)
    assert HUNG_CHECK_SEC == FETCH_TIMEOUT_SEC + LOGIN_TIMEOUT_SEC + HUNG_MARGIN_SEC
    assert HUNG_CHECK_SEC > FETCH_TIMEOUT_SEC + LOGIN_TIMEOUT_SEC
    assert LOGIN_TIMEOUT_SEC > (30 + 60 + 45)  # шаги входа в auth.py укладываются
    assert HUNG_LATE_SEC == 180.0 and STATUS_EVERY_SEC == 30.0


def test_здоровый_цикл_не_считается_зависшим() -> None:
    h = Harness()
    h.loop.next_due = h.clock.monotonic() + 15
    assert h.monitor.diagnose() == ""


def test_проверка_идёт_дольше_порога_строго() -> None:
    h = Harness()
    h.hang_check(HUNG_CHECK_SEC)
    assert h.monitor.diagnose() == ""  # ровно на пороге ещё норма
    h.hang_check(HUNG_CHECK_SEC + 1)
    assert f"проверка идёт {HUNG_CHECK_SEC + 1:.0f} с" in h.monitor.diagnose()


def test_следующая_проверка_опаздывает_строго_больше_порога() -> None:
    h = Harness()
    h.loop.next_due = h.clock.monotonic() - HUNG_LATE_SEC
    assert h.monitor.diagnose() == ""
    h.loop.next_due = h.clock.monotonic() - HUNG_LATE_SEC - 1
    assert "опаздывает на 181 с" in h.monitor.diagnose()


def test_пока_идёт_проверка_опоздание_срока_не_считается() -> None:
    h = Harness()
    h.hang_check(10)
    h.loop.next_due = h.clock.monotonic() - 1000
    assert h.monitor.diagnose() == ""


def test_цикл_ещё_не_запускался_next_due_ноль_не_зависание() -> None:
    h = Harness()
    h.clock.advance(100_000)
    assert h.loop.next_due == 0.0
    assert h.monitor.diagnose() == ""


async def test_пауза_и_долгие_отступления_не_дают_ложной_тревоги() -> None:
    # Каждые 30 с (как работает монитор) диагноз пуст при бессрочной паузе, 15-минутных паузах
    # после 429 и ожидании шага 1
    for script, paused in (
        ([SiteUnavailable("503")], None),
        ([ManualStepRequired("шаг 1")], None),
        ([EMPTY], T0 + timedelta(days=3650)),
    ):
        rig = make_rig(script, paused_until=paused)
        monitor = LivenessMonitor(rig.loop, rig.journal, rig.clock, _noop, _noop_none)
        await rig.start()
        for _ in range(240):  # два часа
            await rig.advance(30)
            assert monitor.diagnose() == ""
        await rig.kill()


async def _noop(text: str) -> None:
    return None


async def _noop_none() -> None:
    return None


# --- tick: восстановление ---


async def test_зависание_одно_восстановление_уведомление_журнал_перезапуск() -> None:
    h = Harness()
    h.hang_check(HUNG_CHECK_SEC + 5)
    await h.monitor.tick()
    assert h.monitor.hung_since == h.clock.now()
    assert h.monitor.recoveries == 1 and h.restarts == 1
    assert len(h.notified) == 1 and "завис" in h.notified[0]
    code, message, level, _ = h.journal.events[0]
    assert (code, level) == ("liveness.hung", "ERROR") and "перезапуск" in message
    status = h.journal.statuses[-1]
    assert status["hung"] is True and "проверка идёт" in str(status["hung_reason"])
    assert status["recoveries"] == 1


async def test_пока_цикл_завис_повторные_тики_не_перезапускают_каждые_30_с() -> None:
    h = Harness()
    h.hang_check(HUNG_CHECK_SEC + 5)
    await h.monitor.tick()
    for _ in range(3):
        h.clock.advance(30)
        await h.monitor.tick()
    assert h.restarts == 1 and len(h.notified) == 1


async def test_если_после_перезапуска_цикл_всё_ещё_завис_восстановление_повторяется() -> None:
    h = Harness()
    h.hang_check(HUNG_CHECK_SEC + 5)
    await h.monitor.tick()
    h.clock.advance(HUNG_RETRY_SEC - 1)
    await h.monitor.tick()
    assert h.restarts == 1
    h.clock.advance(1)
    await h.monitor.tick()
    assert h.restarts == 2 and h.monitor.recoveries == 2
    assert h.monitor.hung_since is not None


async def test_выздоровление_пишется_в_журнал_и_новое_зависание_лечится_заново() -> None:
    h = Harness()
    h.hang_check(HUNG_CHECK_SEC + 5)
    await h.monitor.tick()
    h.heal()
    h.clock.advance(30)
    await h.monitor.tick()
    assert h.monitor.hung_since is None
    assert h.journal.codes() == ["liveness.hung", "liveness.recovered"]
    assert h.journal.statuses[-1]["hung"] is False
    assert h.journal.statuses[-1]["hung_reason"] is None
    h.clock.advance(30)
    h.hang_check(HUNG_CHECK_SEC + 5)
    await h.monitor.tick()
    assert h.restarts == 2 and h.monitor.recoveries == 2


async def test_сбой_уведомления_не_мешает_перезапуску() -> None:
    h = Harness()
    h.notify_error = OSError("Telegram недоступен")
    h.hang_check(HUNG_CHECK_SEC + 5)
    await h.monitor.tick()
    assert h.restarts == 1


async def test_сбой_перезапуска_не_роняет_монитор_и_пишется_в_журнал() -> None:
    h = Harness()
    h.restart_error = RuntimeError("задача не отменилась")
    h.hang_check(HUNG_CHECK_SEC + 5)
    await h.monitor.tick()  # не бросает
    assert "liveness.restart_failed" in h.journal.codes()
    assert h.journal.statuses  # status.json всё равно обновлён: alive_at нужен сторожу


# --- status.json ---


async def test_содержимое_снимка_status_json() -> None:
    h = Harness()
    h.loop.next_due = h.clock.monotonic() + 20
    await h.monitor.tick()
    status = h.journal.statuses[-1]
    assert status["alive_at"] == T0.isoformat()
    assert status["pid"] == os.getpid()
    assert status["version"] == __version__
    assert status["hung"] is False and status["hung_reason"] is None
    assert status["recoveries"] == 0
    assert status["state"] == "running" and status["checks_total"] == 7  # поля цикла


async def test_снимок_дополняется_extra_и_сбой_extra_не_ломает_запись() -> None:
    h = Harness()
    calls = 0

    async def extra() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("БД")
        return {"incident_open": True}

    monitor = LivenessMonitor(h.loop, h.journal, h.clock, h._notify, h._restart, extra)  # type: ignore[arg-type]
    await monitor.tick()
    assert h.journal.statuses[-1]["incident_open"] is True
    await monitor.tick()
    assert len(h.journal.statuses) == 2 and "incident_open" not in h.journal.statuses[-1]
    assert "alive_at" in h.journal.statuses[-1]


async def test_run_обновляет_статус_каждые_30_секунд() -> None:
    clock = ManualClock()
    loop = StubLoop()
    journal = RecordingJournal()
    monitor = LivenessMonitor(loop, journal, clock, _noop, _noop_none)  # type: ignore[arg-type]
    task = asyncio.create_task(monitor.run())
    await settle()
    assert len(journal.statuses) == 1
    await clock.advance(29.9)
    assert len(journal.statuses) == 1
    await clock.advance(0.1)
    await clock.advance(60)
    assert len(journal.statuses) == 4
    assert [s["alive_at"] for s in journal.statuses] == [
        (T0 + timedelta(seconds=n)).isoformat() for n in (0, 30, 60, 90)
    ]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert clock.sleeping == 0


# --- вместе с настоящим циклом опроса и супервизором ---


async def _wire(rig: Rig) -> tuple[Supervisor, LivenessMonitor, list[str]]:
    """Как в runner.py: опрос и монитор под супервизором, перезапуск = сброс источника + restart."""
    supervisor = Supervisor(rig.clock)
    notified: list[str] = []

    async def notify(text: str) -> None:
        notified.append(text)

    async def restart_poll() -> None:
        await rig.loop.reset_source()
        await supervisor.restart("poll")

    monitor = LivenessMonitor(rig.loop, rig.journal, rig.clock, notify, restart_poll)
    supervisor.start("poll", rig.loop.run)
    supervisor.start("liveness", monitor.run)
    await settle()
    return supervisor, monitor, notified


def _hanging(rig: Rig, seconds: float = 10**6):  # type: ignore[no-untyped-def]
    async def hang() -> frozenset:  # type: ignore[type-arg]
        await rig.clock.sleep(seconds)
        return EMPTY

    return hang


async def test_зависшая_проверка_лечится_перезапуском_цикла_и_источника() -> None:
    rig = make_rig()
    rig.source.push(_hanging(rig), EMPTY)
    supervisor, monitor, notified = await _wire(rig)
    await rig.advance(HUNG_CHECK_SEC + 30)
    assert monitor.recoveries == 1
    assert len(notified) == 1
    assert rig.source.closed == 1
    # Зависание замечает ближайший тик монитора после порога
    assert rig.rel(rig.source.starts)[:2] == [0, HUNG_CHECK_SEC + STATUS_EVERY_SEC]
    await rig.advance(30)
    assert monitor.hung_since is None
    assert rig.journal.codes().count("liveness.recovered") == 1
    await supervisor.stop()


async def test_новое_зависание_после_перезапуска_лечится_снова_как_отдельное() -> None:
    rig = make_rig()
    rig.source.push(_hanging(rig), _hanging(rig), EMPTY)
    supervisor, monitor, _ = await _wire(rig)
    cycle = HUNG_CHECK_SEC + STATUS_EVERY_SEC
    await rig.advance(2 * cycle + STATUS_EVERY_SEC)
    assert monitor.recoveries == 2
    assert rig.rel(rig.source.starts)[:3] == [0, cycle, 2 * cycle]
    assert rig.loop.status.checks_total >= 1
    await supervisor.stop()


async def test_медленная_но_живая_проверка_не_перезапускается() -> None:
    rig = make_rig()
    rig.source.push(_hanging(rig, HUNG_CHECK_SEC - 1), EMPTY)
    supervisor, monitor, notified = await _wire(rig)
    await rig.advance(HUNG_CHECK_SEC + 60)
    assert monitor.recoveries == 0 and notified == []
    assert rig.loop.status.checks_total >= 1
    await supervisor.stop()
