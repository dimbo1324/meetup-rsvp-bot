"""Сценарий 9: зависание источника и входа, падение задачи — защита от зависания (R6)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from rcbot.application import poll_loop
from rcbot.domain.models import Channel, PollState, SourceKind
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG-T9: http/client.py:SafeHttp.session + poll_loop.reset_source — после сброса "
        "источника сессия aiohttp создаётся с пустым CookieJar, SessionManager об этом не знает "
        "и повторный вход не делает: следующий запрос уходит без cookies (302) и стоит лишнего "
        "входа из лимита 6 в час (а в окне D5 — 10 минут слепоты)"
    ),
)
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

    await stand.advance(240)  # норма — до 150 с, порог монитора 240 с
    assert stand.liveness.recoveries == 0 and stand.journal.read_status()["hung"] is False
    await stand.advance(30)  # ближайший тик монитора видит 270 с
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

    await stand.advance(270)
    assert stand.liveness.recoveries == 1
    await stand.advance(30)
    assert stand.session.logins >= 1, "после перезапуска вход прошёл: блокировка освобождена"
    assert stand.loop.status.checks_total >= 1
    stuck.set()


# --- задача упала ---


async def test_упавшая_задача_опроса_перезапускается_супервизором_с_паузой_5_секунд(
    make_stand, stand_clock, caplog: pytest.LogCaptureFixture
) -> None:
    source = fake_source(
        stand_clock, RuntimeError("неожиданный сбой"), slots(23), kind=SourceKind.API
    )
    with caplog.at_level(logging.ERROR):
        stand = await make_stand(primary=source, auth=NoLogin())
        assert len(source.calls) == 1 and stand.loop.status.checks_total == 0
        await stand.advance(4.9)
        assert len(source.calls) == 1, "перезапуск не раньше чем через 5 с"
        await stand.advance(0.1)
    assert len(source.calls) == 2 and stand.loop.status.checks_total == 1
    assert any("«poll» упала" in r.getMessage() for r in caplog.records)
    assert all(stand.sent(c) for c in Channel), "после перезапуска цикл работает по-настоящему"


async def test_пауза_перезапуска_растёт_5_10_20(make_stand, stand_clock) -> None:
    boom = RuntimeError("снова")
    source = fake_source(stand_clock, boom, boom, boom, slots(), kind=SourceKind.API)
    stand = await make_stand(primary=source, auth=NoLogin())
    marks = []
    for _ in range(3):
        before = len(source.calls)
        start = stand.elapsed
        while len(source.calls) == before:
            await stand.advance(1)
        marks.append(stand.elapsed - start)
    assert marks == [5, 10, 20]


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
