"""Сценарий 4: 403/429 и серия 5xx — отступление, а не повтор в лоб (SPEC 0.3, правило 4; D4)."""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise

import pytest

from rcbot.application.runtime import K_LADDER_START
from rcbot.domain.models import Channel, PollState
from tests.e2e import site as fake
from tests.e2e.site import FakeSite


def gaps(site: FakeSite, last: int) -> list[float]:
    times = [c.mono for c in site.calls[-last:]]
    return [b - a for a, b in pairwise(times)]


@pytest.mark.parametrize("code", [429, 403])
async def test_отказ_сайта_шаг_назад_по_лестнице_и_пауза_15_минут(
    make_stand, site: FakeSite, code: int
) -> None:
    stand = await make_stand(site=site, ladder_age_hours=7)  # ступень «30 с»
    now = stand.clock.now()
    assert stand.loop.current_interval(now) == 30

    site.push(fake.status(code))
    await stand.next_check()
    t_fail = stand.elapsed
    assert stand.loop.status.state is PollState.BACKOFF
    assert stand.loop.current_interval(stand.clock.now()) == 60, "шаг назад: 30 с → 60 с"
    saved = await stand.storage.kv.get(K_LADDER_START)
    assert datetime.fromisoformat(saved or "") == stand.clock.now(), "ступень сохранена в БД"

    # владелец узнаёт о замедлении, причина названа
    (message,) = stand.texts(Channel.TELEGRAM)
    assert "замедлился" in message and str(code) in message
    assert all(not stand.sent(c) for c in (Channel.NTFY, Channel.CALL))

    # запись в журнал проверок, событий и в БД
    assert stand.journal.read_checks()[-1]["error_kind"] == "RateLimited"
    changed = [e for e in stand.journal.read_events() if e["code"] == "state.changed"]
    assert str(code) in changed[-1]["message"]
    assert any(
        e.code == "state.backoff" and str(code) in e.message
        for e in await stand.storage.events.last(10, "WARNING")
    )

    requests = len(site.calls)
    await stand.advance(899)
    assert len(site.calls) == requests, "15 минут тишины"
    await stand.advance(1)
    assert len(site.calls) == requests + 1
    assert stand.elapsed - t_fail == pytest.approx(900)
    assert stand.loop.status.state is PollState.RUNNING
    assert stand.loop.status.interval_sec == 60
    assert "Опрос снова работает" in stand.texts(Channel.TELEGRAM)[-1]


async def test_страница_блокировки_шлюза_считается_отказом_сайта(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    site.push(fake.html(fake.BLOCKED_HTML))
    await stand.next_check()
    assert stand.loop.status.state is PollState.BACKOFF
    assert stand.journal.read_checks()[-1]["error_kind"] == "RateLimited"


async def test_серия_503_пауза_растёт_30_60_120_backoff_со_второго_подряд(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    site.push(fake.status(503), times=3)

    await stand.next_check()  # первый сбой
    assert stand.loop.status.state is PollState.RUNNING, "одиночный сбой не беспокоит владельца"
    assert stand.texts(Channel.TELEGRAM) == []
    await stand.advance(30)
    assert stand.loop.status.state is PollState.BACKOFF
    await stand.advance(60 + 120)  # третий сбой и успешная проверка
    assert stand.loop.status.state is PollState.RUNNING

    assert gaps(site, 5) == [60, 30, 60, 120]  # 60 до сбоя, затем 30, 60, 120
    messages = stand.texts(Channel.TELEGRAM)
    assert [("замедлился" in m, "снова работает" in m) for m in messages] == [
        (True, False),
        (False, True),
    ]


async def test_пауза_при_5xx_удваивается_до_15_минут_и_не_выше(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    site.push(fake.status(500), times=8)
    await stand.next_check()
    for _ in range(7):
        await stand.next_check()
    assert gaps(site, 9)[1:8] == [30, 60, 120, 240, 480, 900, 900]


async def test_отказ_после_восстановления_начинает_лесенку_заново(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    site.push(fake.status(503), times=2)
    await stand.checks(3)  # два сбоя и успех
    site.push(fake.status(503))
    await stand.next_check()
    await stand.next_check()
    assert gaps(site, 3)[-1] == 30, "после успеха пауза снова начинается с 30 с"


async def test_сайт_отвечает_только_на_get(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    site.push(fake.status(429), fake.status(503))
    await stand.checks(4)
    assert {r.method for r in site.server.requests} == {"GET"}
