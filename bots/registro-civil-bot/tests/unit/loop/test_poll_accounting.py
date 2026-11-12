"""Учёт цикла опроса: почасовая статистика, статус, status.json, журнал и события состояний
(SPEC 2.2 п. 7, 2.7: в БД не каждая проверка, а одна строка в час)."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta

from tests.fakes import slots
from tests.unit.loop.harness import make_rig

from rcbot.domain.errors import LoginFailed, SessionExpired, SiteUnavailable
from rcbot.domain.events import PollerStateChanged
from rcbot.domain.models import PollState, SourceKind

EMPTY: frozenset = frozenset()  # type: ignore[type-arg]
HOUR_15 = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
HOUR_16 = HOUR_15 + timedelta(hours=1)


# --- почасовая статистика ---


async def test_статистика_копится_в_памяти_и_пишется_при_смене_часа() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.advance(3599)  # последняя проверка часа — в 15:59:00
    assert rig.stats.saved == {}  # в БД ничего: только счётчик в памяти (SPEC 2.7)
    hour = rig.loop.current_hour()
    assert hour is not None and hour.hour == HOUR_15 and hour.checks == 60
    await rig.advance(1)  # 16:00:00 — новый час
    assert set(rig.stats.saved) == {HOUR_15}
    saved = rig.stats.saved[HOUR_15]
    assert saved.checks == 60
    assert saved.latencies_ms == [5.0] * 60
    assert saved.percentile(0.95) == 5.0
    current = rig.loop.current_hour()
    assert current is not None and current.hour == HOUR_16 and current.checks == 1
    await rig.kill()


async def test_проверки_в_БД_не_пишутся_по_одной() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.advance(3 * 3600 - 1)
    # За три часа — не больше двух готовых строк, а не 180 проверок
    assert len(rig.stats.saved) == 2
    await rig.kill()


async def test_остановка_сохраняет_неполный_час() -> None:
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.advance(120)
    await rig.stop()
    assert rig.stats.saved[HOUR_15].checks == 3


async def test_отмена_задачи_тоже_сохраняет_неполный_час() -> None:
    # Супервизор отменяет run() при остановке и перезапуске: статистика часа не должна теряться
    rig = make_rig([EMPTY], base_interval=60.0)
    await rig.start()
    await rig.advance(60)
    await rig.kill()
    assert rig.stats.saved[HOUR_15].checks == 2


async def test_сбой_записи_статистики_при_остановке_не_роняет_остановку() -> None:
    rig = make_rig([EMPTY])
    await rig.start()

    async def broken(stats: object) -> None:
        raise OSError("диск")

    rig.stats.save_hour = broken  # type: ignore[method-assign]
    await rig.stop()
    assert rig.task is not None and rig.task.exception() is None


async def test_ошибки_и_входы_считаются_отдельно_от_проверок() -> None:
    rig = make_rig([SiteUnavailable("503"), SessionExpired("x"), EMPTY])
    await rig.start()
    await rig.advance(1000)
    hour = rig.loop.current_hour()
    assert hour is not None
    assert hour.errors == 1  # только SiteUnavailable: истёкшая сессия не «ошибка сайта»
    assert hour.relogins == 1
    assert hour.checks == len(rig.source.starts) - 2
    await rig.kill()


async def test_вход_по_команде_попадает_в_статистику_один_раз() -> None:
    rig = make_rig([EMPTY])
    await rig.start()
    await rig.loop.relogin()
    await rig.advance(30)  # проверка после входа не должна засчитать его второй раз
    hour = rig.loop.current_hour()
    assert hour is not None and hour.relogins == 1
    await rig.kill()


async def test_неудачный_вход_по_команде_не_считается_входом() -> None:
    rig = make_rig([EMPTY], failures=[LoginFailed("нет")])
    await rig.start()
    with contextlib.suppress(LoginFailed):
        await rig.loop.relogin()
    hour = rig.loop.current_hour()
    assert hour is not None and hour.relogins == 0
    await rig.kill()


# --- статус и status.json ---


async def test_статус_после_успешных_проверок() -> None:
    rig = make_rig([EMPTY], base_interval=20.0)
    await rig.start()
    await rig.advance(40)
    status = rig.loop.status
    assert status.checks_total == 3
    assert status.last_check_at == HOUR_15 + timedelta(seconds=40)
    assert status.last_latency_ms == 5.0
    assert status.recent_latencies == [5.0, 5.0, 5.0]
    assert status.interval_sec == 20.0
    assert status.next_check_in_sec == 20.0
    assert status.errors_total == 0 and status.last_error == ""
    assert status.state is PollState.RUNNING
    await rig.kill()


async def test_snapshot_для_status_json() -> None:
    rig = make_rig(base_interval=20.0, months=4)

    async def slow() -> frozenset:  # type: ignore[type-arg]
        await rig.clock.sleep(5)
        return EMPTY

    rig.source.push(slow, SiteUnavailable("503"))
    await rig.start()
    await rig.advance(2)
    mid = rig.loop.snapshot()
    assert mid["check_in_progress_sec"] == 2.0
    assert mid["source"] == "api" and mid["state"] == "running"
    assert mid["months_ahead"] == 4 and mid["base_interval_sec"] == 20.0
    assert mid["paused_until"] is None and mid["last_check_at"] is None
    await rig.advance(3)
    done = rig.loop.snapshot()
    assert done["check_in_progress_sec"] is None
    assert done["next_check_in_sec"] == 15.0
    assert done["last_check_at"] == (HOUR_15 + timedelta(seconds=5)).isoformat()
    assert done["started_at"] == HOUR_15.isoformat()
    assert done["checks_total"] == 1
    await rig.advance(15)
    failed = rig.loop.snapshot()
    assert failed["errors_total"] == 1 and failed["last_error"] == "503"
    assert failed["last_error_at"] == (HOUR_15 + timedelta(seconds=20)).isoformat()
    json.dumps(failed)  # снимок обязан сериализоваться в JSON
    await rig.kill()


async def test_snapshot_при_паузе_показывает_дату_а_после_паузы_нет() -> None:
    until = HOUR_15 + timedelta(seconds=100)
    rig = make_rig([EMPTY], paused_until=until)
    await rig.start()
    assert rig.loop.snapshot()["paused_until"] == until.isoformat()
    assert rig.loop.snapshot()["state"] == "paused"
    await rig.advance(100)
    assert rig.loop.snapshot()["paused_until"] is None
    await rig.kill()


async def test_snapshot_DomSource_показывает_минимум_60_с() -> None:
    rig = make_rig([EMPTY], kind=SourceKind.DOM, base_interval=20.0)
    assert rig.loop.snapshot()["interval_sec"] == 60.0
    assert rig.loop.snapshot()["source"] == "dom"


# --- журнал проверок ---


async def test_запись_проверки_для_журнала_наблюдения() -> None:
    rig = make_rig([slots(23, 24), SiteUnavailable("503")], base_interval=20.0)
    await rig.start()
    await rig.advance(20)
    ok, failed = rig.journal.checks
    assert ok.ok and ok.slots == slots(23, 24) and ok.latency_ms == 5.0
    assert ok.state == "running" and ok.interval_sec == 20.0 and ok.source is SourceKind.API
    assert ok.error_kind == "" and ok.error == ""
    assert ok.at == HOUR_15
    assert not failed.ok and failed.slots == frozenset()
    assert failed.error_kind == "SiteUnavailable" and failed.at == HOUR_15 + timedelta(seconds=20)
    await rig.kill()


# --- события состояний ---


async def test_событие_состояния_только_при_реальной_смене() -> None:
    rig = make_rig([EMPTY], base_interval=20.0)
    await rig.start()
    await rig.advance(200)
    assert rig.of_type(PollerStateChanged) == []  # RUNNING → RUNNING — не событие
    assert rig.event_log.events == []
    await rig.kill()


async def test_пауза_и_возобновление_дают_по_одному_событию_и_записи_в_журнал() -> None:
    rig = make_rig([EMPTY])
    await rig.start()
    await rig.loop.pause(HOUR_15 + timedelta(days=1))
    rig.loop.wake()
    await rig.advance(10)
    rig.loop.wake()
    await rig.advance(10)
    assert [(e.old, e.new) for e in rig.of_type(PollerStateChanged)] == [
        (PollState.RUNNING, PollState.PAUSED)
    ]
    await rig.loop.resume()
    await rig.advance(0)
    assert [(e.old, e.new) for e in rig.of_type(PollerStateChanged)][-1] == (
        PollState.PAUSED,
        PollState.RUNNING,
    )
    codes = [(e.code, e.level) for e in rig.event_log.events]
    assert codes == [("state.paused", "INFO"), ("state.running", "INFO")]
    await rig.kill()


async def test_журнал_событий_пишет_BACKOFF_как_предупреждение_с_причиной() -> None:
    rig = make_rig([SiteUnavailable("API ответил 503")])
    await rig.start()
    await rig.advance(30)
    [event] = rig.event_log.events
    assert (event.code, event.level, event.message) == (
        "state.backoff",
        "WARNING",
        "API ответил 503",
    )
    assert event.ts == HOUR_15 + timedelta(seconds=30)
    await rig.kill()


async def test_сбой_записи_события_в_журнал_не_роняет_опрос() -> None:
    rig = make_rig([SiteUnavailable("503")])

    async def broken(event: object) -> None:
        raise OSError("диск")

    rig.event_log.write = broken  # type: ignore[method-assign]
    await rig.start()
    await rig.advance(30)
    assert rig.task is not None and not rig.task.done()
    assert rig.loop.status.state is PollState.BACKOFF
    await rig.kill()


async def test_событие_слотов_и_состояния_идут_в_шину_в_порядке_возникновения() -> None:
    from rcbot.domain.events import SlotsAppeared

    rig = make_rig([SiteUnavailable("a"), SiteUnavailable("b"), slots(23)])
    await rig.start()
    await rig.advance(90)
    kinds = [type(e) for e in rig.events]
    assert kinds == [PollerStateChanged, PollerStateChanged, SlotsAppeared]
    await rig.kill()
