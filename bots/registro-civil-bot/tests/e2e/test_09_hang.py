"""Сценарий 9: зависание источника и входа, падение задачи — защита от зависания (R6)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from itertools import pairwise

import pytest

from rcbot.application import poll_loop
from rcbot.application.liveness import HUNG_CHECK_SEC
from rcbot.domain.models import Channel, PollState, SourceKind
from tests.e2e.helpers import MIN_GAP
from tests.e2e.site import FakeSite, SiteAuthenticator
from tests.e2e.stand import fake_source
from tests.fakes import slots


class NoLogin:
    async def login(self) -> list:
        return []


def hanging(event: asyncio.Event):
    async def hang() -> frozenset:
        await event.wait()
        return slots()

    return hang


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        poll_loop, "FETCH_TIMEOUT_SEC", 0.05
    )  # настоящее время: см. asyncio.timeout


# --- проверка «виснет» на сайте ---


async def test_зависшая_проверка_прерывается_источник_пересоздаётся_пауза_30_секунд(
    make_stand, site: FakeSite, short_timeout
) -> None:
    stand = await make_stand(site=site)
    site.push(site.hang())
    await stand.next_check()

    check = stand.journal.read_checks()[-1]
    assert check["ok"] is False and check["error_kind"] == "TimeoutError"
    events = stand.journal.read_events()
    assert any(e["code"] == "check.timeout" and e["level"] == "WARNING" for e in events)
    http = stand.primary._http
    assert http._session is None or http._session.closed, "reset_source закрыл соединения"
    t = stand.elapsed
    await stand.next_check()
    assert stand.elapsed - t == pytest.approx(30)
    await stand.advance(MIN_GAP)  # если сессия потерялась, повторная проверка идёт после входа
    assert stand.journal.read_checks()[-1]["ok"] is True


async def test_два_зависания_подряд_уводят_в_backoff_и_сообщают(
    make_stand, site: FakeSite, short_timeout
) -> None:
    stand = await make_stand(site=site)
    site.push(site.hang(), site.hang())
    await stand.checks(2)
    assert stand.loop.status.state is PollState.BACKOFF
    assert any("не уложилась" in m for m in stand.texts(Channel.TELEGRAM))
    await stand.checks(1)
    assert stand.loop.status.state is PollState.RUNNING


async def test_после_сброса_зависшего_источника_сессия_не_теряется(
    make_stand, site: FakeSite, short_timeout
) -> None:
    stand = await make_stand(site=site)
    site.push(site.hang())
    await stand.next_check()
    await stand.next_check()

    assert stand.session.logins == 1, "сброс соединений не должен стоить входа"
    assert site.calls[-1].cookie.startswith("simple_bpm_session=SESSION-1")


# --- цикл опроса встал: монитор живости ---


async def test_монитор_живости_замечает_зависший_цикл_и_перезапускает_задачу(
    make_stand, stand_clock
) -> None:
    release = asyncio.Event()
    source = fake_source(stand_clock, hanging(release), slots(23), kind=SourceKind.API)
    stand = await make_stand(primary=source, auth=NoLogin(), ignore={"tick"})
    old_task = stand.supervisor._tasks["poll"]
    assert stand.loop.tick_started_at is not None, "проверка идёт и не заканчивается"

    await stand.advance(HUNG_CHECK_SEC)  # норма — до запроса и входа, дальше порог монитора
    assert stand.liveness.recoveries == 0 and stand.journal.read_status()["hung"] is False
    await stand.advance(30)  # ближайший тик монитора видит время за порогом
    assert stand.liveness.recoveries == 1
    assert stand.journal.read_status()["hung"] is True

    new_task = stand.supervisor._tasks["poll"]
    assert new_task is not old_task and old_task.done(), "задача настоящая, а не заглушка"
    assert not new_task.done()
    assert source.closed >= 1, "источник (браузер) закрыт и создастся заново"
    assert any("завис" in m for m in stand.texts(Channel.TELEGRAM))
    codes = stand.journal.codes()
    assert "liveness.hung" in codes

    await stand.advance(60)  # цикл снова проверяет; монитор отмечает выздоровление
    assert len(source.calls) >= 2 and stand.loop.status.checks_total >= 1
    assert "liveness.recovered" in stand.journal.codes()
    assert stand.journal.read_status()["hung"] is False
    release.set()


async def test_зависший_вход_тоже_ловится_и_блокировка_входа_не_залипает(
    make_stand, site: FakeSite
) -> None:
    stuck = asyncio.Event()
    calls = 0

    async def first_login_hangs() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await stuck.wait()

    auth = SiteAuthenticator(site, delay=first_login_hangs)
    stand = await make_stand(site=site, auth=auth, ignore={"tick"})
    assert stand.session.logins == 0, "первый вход завис"

    await stand.advance(HUNG_CHECK_SEC + 30)
    assert stand.liveness.recoveries == 1
    await stand.advance(30)
    assert stand.session.logins >= 1, "после перезапуска вход прошёл: блокировка освобождена"
    assert stand.loop.status.checks_total >= 1
    stuck.set()


# --- задача упала ---


def check_times(stand) -> list[datetime]:
    return [datetime.fromisoformat(c["at"]) for c in stand.journal.read_checks()]


async def test_исключение_источника_не_роняет_цикл_а_ведёт_в_backoff_30_60_120(
    make_stand, stand_clock
) -> None:
    boom = RuntimeError("секретный-текст-сбоя")
    source = fake_source(stand_clock, boom, boom, boom, slots(23), kind=SourceKind.API)
    stand = await make_stand(primary=source, auth=NoLogin())
    task = stand.supervisor._tasks["poll"]
    await stand.checks(3)

    times = check_times(stand)
    assert [(b - a).total_seconds() for a, b in pairwise(times)] == [30, 60, 120]
    assert not task.done() and stand.supervisor._tasks["poll"] is task, "задача жива и та же"
    assert stand.loop.status.checks_total == 1 and all(stand.sent(c) for c in Channel)
    crashes = [e for e in stand.journal.read_events() if e["code"] == "check.crash"]
    assert len(crashes) == 3 and all(e["level"] == "ERROR" for e in crashes)
    assert all("RuntimeError" in e["message"] for e in crashes)
    assert "секретный-текст-сбоя" not in str(
        stand.journal.read_events() + stand.journal.read_checks()
    )
    assert source.closed >= 3, "источник пересоздаётся после каждого сбоя"


async def test_сбой_самого_цикла_ловится_и_опрос_продолжается(make_stand, stand_clock) -> None:
    source = fake_source(stand_clock, slots(), slots(23), kind=SourceKind.API)
    stand = await make_stand(primary=source, auth=NoLogin())
    real_compare = stand.detector.compare
    failures = [ValueError("внутренний сбой")]

    def flaky(snapshot):
        if failures:
            raise failures.pop()
        return real_compare(snapshot)

    stand.detector.compare = flaky  # type: ignore[method-assign]
    await stand.checks(3)

    assert "poll.crash" in stand.journal.codes()
    assert not stand.supervisor._tasks["poll"].done()
    assert all(stand.sent(c) for c in Channel), "после сбоя цикл дошёл до тревоги"


async def test_супервизор_перезапускает_упавшую_задачу_с_паузой_5_10_20_секунд(
    make_stand, stand_clock, caplog: pytest.LogCaptureFixture
) -> None:
    source = fake_source(stand_clock, slots(), kind=SourceKind.API)
    stand = await make_stand(primary=source, auth=NoLogin())
    starts: list[float] = []

    async def canary() -> None:
        starts.append(stand.elapsed)
        if len(starts) <= 3:
            raise RuntimeError("задача упала")

    with caplog.at_level(logging.ERROR):
        stand.supervisor.start("canary", canary)
        await stand.advance(40)

    assert [round(b - a) for a, b in pairwise(starts)] == [5, 10, 20]
    assert sum("«canary» упала" in r.getMessage() for r in caplog.records) == 3


async def test_остановка_не_оставляет_висящих_задач(make_stand, stand_clock) -> None:
    before = asyncio.all_tasks()
    source = fake_source(stand_clock, slots(), slots(23), kind=SourceKind.API)
    stand = await make_stand(primary=source, auth=NoLogin())
    await stand.checks(2)
    assert stand.alerts.repeating()
    await stand.stop()
    await asyncio.sleep(0)
    leftovers = {t for t in asyncio.all_tasks() - before if not t.done()}
    assert not leftovers, [t.get_name() for t in leftovers]
