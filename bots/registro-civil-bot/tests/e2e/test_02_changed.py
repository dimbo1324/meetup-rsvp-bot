"""Сценарий 2: изменение набора мест внутри открытого инцидента (SPEC 2.3, модель инцидента)."""

from __future__ import annotations

import pytest

from rcbot.domain.events import SlotsChanged
from rcbot.domain.models import AlertKind, Channel
from tests.e2e.helpers import D23, D24, collect_events, slot
from tests.e2e.site import FakeSite
from tests.e2e.stand import Stand

# Интервал 45 с и «старая» лестница: проверки не совпадают по времени с повторами (60/120/180)
FAST = {"ladder_age_hours": 20, "poll_interval_sec": 45}


async def opened(make_stand, site: FakeSite) -> Stand:
    stand = await make_stand(site=site, **FAST)
    site.slots = list(D23)
    await stand.next_check()  # t = 45: тревога
    await stand.advance(60)  # t = 105: Telegram повторил первую тревогу
    assert stand.notifiers[Channel.TELEGRAM].attempts == [1, 2]
    return stand


async def test_новые_слоты_дают_новую_волну_и_отменяют_прежние_повторы(
    make_stand, site: FakeSite
) -> None:
    stand = await opened(make_stand, site)
    events = collect_events(stand)
    site.slots = D23 + D24
    await stand.next_check()  # t = 135
    t_change = stand.elapsed

    (changed,) = [e for e in events if isinstance(e, SlotsChanged)]
    assert changed.added == frozenset({slot(24, 9, 0)})
    for channel in Channel:
        last = stand.sent(channel)[-1]
        assert last.attempt == 1 and last.alert.kind is AlertKind.CHANGED, channel
        assert last.mono - stand.t0 == pytest.approx(t_change), channel
    assert "Новые: 24.02" in stand.texts(Channel.TELEGRAM)[-1]

    await stand.advance(400)
    for channel in Channel:
        later = [s for s in stand.sent(channel) if s.mono - stand.t0 >= t_change]
        assert {s.alert.kind for s in later} == {AlertKind.CHANGED}, (
            f"{channel}: прежние повторы должны быть отменены"
        )
    # ntfy: новая серия 0, 120, 240 с от нового события; старый повтор на t = 165 не прошёл
    marks = [s.mono - stand.t0 - t_change for s in stand.sent(Channel.NTFY)]
    assert [m for m in marks if m >= 0] == [0, 120, 240, 360]


async def test_изменение_записано_в_инцидент_а_не_в_новый(make_stand, site: FakeSite) -> None:
    stand = await opened(make_stand, site)
    site.slots = D23 + D24
    await stand.next_check()

    (incident,) = await stand.incidents()
    assert incident.id == 1 and incident.status.value == "open"
    assert slot(24, 9, 0) in incident.slots and len(incident.slots) == 4
    deliveries = await stand.storage.incidents.deliveries_for(1)
    assert {d.incident_id for d in deliveries} == {1}
    assert [r["событие"] for r in stand.journal.read_openings()] == ["появились", "добавились"]
    assert "slots.changed" in stand.journal.codes()


async def test_пропажа_части_слотов_не_вызывает_тревоги(make_stand, site: FakeSite) -> None:
    stand = await opened(make_stand, site)
    events = collect_events(stand)
    first_waves = {c: sum(1 for s in stand.sent(c) if s.attempt == 1) for c in Channel}

    site.slots = D23[:1]  # осталось одно из трёх
    await stand.checks(3)

    assert not [e for e in events if isinstance(e, SlotsChanged)]
    for channel in Channel:
        assert sum(1 for s in stand.sent(channel) if s.attempt == 1) == first_waves[channel]
    assert (await stand.incidents())[0].status.value == "open"
    assert [r["событие"] for r in stand.journal.read_openings()] == ["появились"]


async def test_тот_же_набор_слотов_не_повторяет_тревогу(make_stand, site: FakeSite) -> None:
    stand = await opened(make_stand, site)
    events = collect_events(stand)
    await stand.checks(5)  # сайт отдаёт те же места, отпечаток не меняется
    assert events == []
    assert sum(1 for s in stand.sent(Channel.TELEGRAM) if s.attempt == 1) == 1
