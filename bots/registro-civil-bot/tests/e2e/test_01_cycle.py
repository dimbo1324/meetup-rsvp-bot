"""Сценарий 1 (SPEC 7): «нет мест → есть → пропали» — от ответа сайта до записи в журнал и БД."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rcbot.domain.events import SlotsAppeared, SlotsGone
from rcbot.domain.models import Channel, PollState
from tests.e2e.helpers import D23, collect_events, slot, slow_channels
from tests.e2e.site import FakeSite
from tests.e2e.stand import Stand

EVERY = {Channel.TELEGRAM: 60, Channel.NTFY: 120, Channel.CALL: 180}


async def open_incident(stand: Stand, site: FakeSite) -> float:
    """Сайт показывает места; возвращает секунды стенда, когда проверка их увидела."""
    site.slots = list(D23)
    await stand.next_check()
    return stand.elapsed


async def test_первая_проверка_входит_на_сайт_и_видит_пустой_календарь(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    # нет cookies → 302 → вход → немедленная повторная проверка уже с cookies
    assert [c.cookie == "" for c in site.calls] == [True, False]
    assert stand.session.logins == 1
    assert stand.loop.status.state is PollState.RUNNING
    assert stand.loop.status.checks_total == 1
    assert await stand.incidents() == []
    assert all(not stand.sent(c) for c in Channel)
    codes = stand.journal.codes()
    assert codes.count("state.changed") == 2  # running → reauth → running
    assert "login.start" in codes and "login.ok" in codes


async def test_появление_мест_одна_первая_волна_во_все_три_канала_параллельно(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site, notifiers=slow_channels(0.02))
    events = collect_events(stand)
    t_seen = await open_incident(stand, site)

    assert t_seen == pytest.approx(60)
    appeared = [e for e in events if isinstance(e, SlotsAppeared)]
    assert len(appeared) == 1
    assert appeared[0].snapshot.slots == frozenset(
        {slot(23, 10, 30), slot(23, 10, 45), slot(23, 11, 0)}
    )
    sent = {c: stand.sent(c) for c in Channel}
    for channel, items in sent.items():
        assert len(items) == 1 and items[0].attempt == 1, channel
        assert items[0].mono - stand.t0 == pytest.approx(60), channel
    # параллельно: пока один канал ещё отправляет, остальные уже начали
    starts = [items[0].perf_start for items in sent.values()]
    ends = [items[0].perf_end for items in sent.values()]
    assert max(starts) < min(ends)
    telegram = sent[Channel.TELEGRAM][0].alert
    assert "23.02.2027" in telegram.text and "10:30, 10:45, 11:00" in telegram.text


async def test_инцидент_и_доставки_записаны_в_базу_с_номером_инцидента(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    await open_incident(stand, site)

    (incident,) = await stand.incidents()
    assert incident.id == 1 and incident.status.value == "open"
    assert incident.opened_at == stand.clock.now()  # часы стенда за проверку не двигались
    assert incident.slots == frozenset({slot(23, 10, 30), slot(23, 10, 45), slot(23, 11, 0)})
    deliveries = await stand.storage.incidents.deliveries_for(incident.id)
    assert sorted(d.channel for d in deliveries) == sorted(Channel)
    assert all(d.ok and d.attempt == 1 and d.incident_id == incident.id for d in deliveries)


async def test_повторы_идут_по_расписанию_60_120_180(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    t_wave = await open_incident(stand, site)

    await stand.advance(180)  # до t_wave + 180
    for channel, every in EVERY.items():
        marks = [s.mono - stand.t0 - t_wave for s in stand.sent(channel)]
        assert marks == [every * k for k in range(len(marks))], channel
    assert stand.notifiers[Channel.TELEGRAM].attempts == [1, 2, 3, 4]
    assert stand.notifiers[Channel.NTFY].attempts == [1, 2]
    assert stand.notifiers[Channel.CALL].attempts == [1, 2]
    # каждый повтор тоже записан в БД с номером инцидента
    deliveries = await stand.storage.incidents.deliveries_for(1)
    assert len(deliveries) == 4 + 2 + 2
    assert {d.incident_id for d in deliveries} == {1}


async def test_вижу_останавливает_повторы_во_всех_каналах_инцидент_остаётся_открытым(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    await open_incident(stand, site)
    await stand.advance(120)
    before = {c: len(stand.sent(c)) for c in Channel}
    assert stand.alerts.repeating()

    assert await stand.controller.ack() is True
    await stand.advance(3 * 3600)  # любое число повторов успело бы набежать

    assert {c: len(stand.sent(c)) for c in Channel} == before
    assert not stand.alerts.repeating()
    incident = await stand.book.current()
    assert incident is not None and incident.acked
    (stored,) = await stand.incidents()
    assert stored.status.value == "open" and stored.acked_at is not None
    assert "alert.ack" in stand.journal.codes()


async def test_три_пустые_проверки_подряд_закрывают_инцидент(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    events = collect_events(stand)
    t_seen = await open_incident(stand, site)
    site.slots = []

    await stand.checks(2)
    assert not [e for e in events if isinstance(e, SlotsGone)]
    assert (await stand.incidents())[0].status.value == "open"

    await stand.next_check()
    t_gone = stand.elapsed
    assert len([e for e in events if isinstance(e, SlotsGone)]) == 1
    (incident,) = await stand.incidents()
    assert incident.status.value == "closed"
    assert incident.closed_at == stand.clock.now()
    # служебное «места пропали» — только в Telegram, без звонка и push
    assert "Места пропали" in stand.texts(Channel.TELEGRAM)[-1]
    for channel in (Channel.NTFY, Channel.CALL):
        assert all(s.alert.kind.value == "appeared" for s in stand.sent(channel))
    assert not stand.alerts.repeating()
    after = {c: len(stand.sent(c)) for c in Channel}
    await stand.advance(3600)  # повторы отменены: ни одной отправки после закрытия
    assert {c: len(stand.sent(c)) for c in Channel} == after

    rows = stand.journal.read_openings()
    assert [r["событие"] for r in rows] == ["появились", "пропали"]
    assert rows[0]["даты"] == "2027-02-23" and rows[0]["источник"] == "api"
    assert float(rows[1]["окно_открыто_мин"]) == pytest.approx((t_gone - t_seen) / 60, abs=0.05)


async def test_дребезг_две_пустые_проверки_не_закрывают_инцидент(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    await open_incident(stand, site)
    site.slots = []
    await stand.checks(2)
    site.slots = list(D23)  # места вернулись до третьей пустой проверки
    await stand.checks(3)
    site.slots = []
    await stand.checks(2)

    (incident,) = await stand.incidents()
    assert incident.status.value == "open", "серия пустых проверок должна начинаться заново"
    assert [r["событие"] for r in stand.journal.read_openings()] == ["появились"]


async def test_каждая_проверка_записана_в_журнал_проверок(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    await open_incident(stand, site)
    site.slots = []
    await stand.checks(3)

    checks = stand.journal.read_checks()
    assert len(checks) == len(site.calls), "строка журнала на каждый запрос к сайту"
    assert checks[0]["ok"] is False and checks[0]["error_kind"] == "SessionExpired"
    assert all(c["ok"] for c in checks[1:])
    assert all(c["source"] == "api" for c in checks)
    assert [c["slots_count"] for c in checks[1:]] == [0, 3, 0, 0, 0]
    assert checks[2]["slots"] == ["2027-02-23 10:30", "2027-02-23 10:45", "2027-02-23 11:00"]
    assert all(c["at"].endswith("-03:00") for c in checks), "время — в часовом поясе настроек"


async def test_status_json_обновляется_и_показывает_инцидент(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    await open_incident(stand, site)
    await stand.advance(30)  # тик монитора живости

    status = stand.journal.read_status()
    alive = datetime.fromisoformat(status["alive_at"])
    assert timedelta(0) <= stand.clock.now() - alive < timedelta(seconds=30)
    assert status["state"] == "running" and status["hung"] is False
    assert status["incident_open"] is True and status["incident_days"] == ["2027-02-23"]
    assert status["alert_repeating"] is True
    assert status["checks_total"] == stand.loop.status.checks_total >= 2

    await stand.advance(30)
    later = datetime.fromisoformat(stand.journal.read_status()["alive_at"])
    assert later - alive == timedelta(seconds=30), "метка жизни двигается каждые 30 с"
