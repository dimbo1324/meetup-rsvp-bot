"""Сценарий 7: перезапуск при открытом инциденте — детектор продолжает его, а не тревожит заново."""

from __future__ import annotations

from datetime import timedelta

import pytest

from rcbot.domain.events import SlotsChanged
from rcbot.domain.models import Channel
from tests.e2e.helpers import D23, D24, collect_events, slot, slow_channels
from tests.e2e.site import FakeSite
from tests.e2e.stand import Stand


async def with_open_incident(make_stand, site: FakeSite, *, graceful: bool) -> Stand:
    first = await make_stand(site=site)
    site.slots = list(D23)
    await first.next_check()
    assert (await first.incidents())[0].status.value == "open"
    await (first.stop() if graceful else first.crash())
    return first


@pytest.mark.parametrize("graceful", [True, False], ids=["штатная-остановка", "обрыв"])
async def test_те_же_места_после_перезапуска_без_повторной_тревоги(
    make_stand, site: FakeSite, graceful: bool
) -> None:
    await with_open_incident(make_stand, site, graceful=graceful)

    second = await make_stand(site=site)  # новый процесс на той же БД
    await second.checks(3)
    await second.advance(3 * 3600)  # и прежние повторы не возобновились

    assert all(not second.sent(c) for c in Channel), "ни одной тревоги от нового процесса"
    (incident,) = await second.incidents()
    assert incident.id == 1 and incident.status.value == "open"
    assert (await second.book.current()) is not None
    assert second.detector.incident_open


async def test_новые_места_после_перезапуска_дают_изменение_в_том_же_инциденте(
    make_stand, site: FakeSite
) -> None:
    await with_open_incident(make_stand, site, graceful=True)
    second = await make_stand(site=site)
    events = collect_events(second)
    site.slots = D23 + D24
    await second.next_check()

    (changed,) = [e for e in events if isinstance(e, SlotsChanged)]
    assert changed.added == frozenset({slot(24, 9, 0)})
    assert all(second.sent(c)[-1].alert.kind.value == "changed" for c in Channel)
    (incident,) = await second.incidents()
    assert incident.id == 1 and slot(24, 9, 0) in incident.slots


async def test_места_пропали_после_перезапуска_инцидент_закрывается(
    make_stand, site: FakeSite
) -> None:
    await with_open_incident(make_stand, site, graceful=True)
    second = await make_stand(site=site)
    site.slots = []
    await second.checks(3)

    (incident,) = await second.incidents()
    assert incident.status.value == "closed"
    assert not second.detector.incident_open
    assert await second.book.current() is None
    assert "Места пропали" in second.texts(Channel.TELEGRAM)[-1]


async def test_подтверждение_вижу_переживает_перезапуск(make_stand, site: FakeSite) -> None:
    first = await make_stand(site=site)
    site.slots = list(D23)
    await first.next_check()
    assert await first.controller.ack()
    await first.stop()

    second = await make_stand(site=site)
    incident = await second.book.current()
    assert incident is not None and incident.acked
    assert await second.controller.ack() is True  # повторное «Вижу» безвредно
    assert all(not second.sent(c) for c in Channel)


async def test_настройки_из_telegram_переживают_перезапуск(make_stand, site: FakeSite) -> None:
    first = await make_stand(site=site, ladder_age_hours=20)
    await first.controller.set_interval(90)
    await first.controller.toggle_channel(Channel.NTFY)
    await first.controller.set_months(9)
    until = await first.controller.pause(30)
    await first.crash()

    second = await make_stand(site=site)
    runtime = second.runtime
    assert runtime.base_interval_sec == 90 and runtime.months_ahead == 9
    assert runtime.channels_off == {Channel.NTFY}
    assert runtime.paused_until == until
    # пауза действует и в новом процессе: до её конца запросов нет
    calls = len(site.calls)
    await second.advance(29 * 60)
    assert len(site.calls) == calls
    await second.advance(2 * 60)
    assert len(site.calls) > calls


async def test_штатная_остановка_сохраняет_статистику_текущего_часа(
    make_stand, site: FakeSite
) -> None:
    first = await make_stand(site=site)
    await first.checks(3)
    ok_checks = first.loop.status.checks_total
    assert ok_checks >= 3
    await first.stop()

    second = await make_stand(site=site)
    hours = await second.storage.stats.since(second.clock.now() - timedelta(hours=2))
    assert sum(h.checks for h in hours) == ok_checks, "статистика незавершённого часа не потеряна"


async def test_статистика_часа_сохраняется_если_сообщение_об_остановке_идёт_дольше_записи(
    make_stand, site: FakeSite
) -> None:
    """Тот же сценарий, но Telegram отвечает за 50 мс: запись успевает — это и есть гонка."""
    first = await make_stand(site=site, notifiers=slow_channels(0.05))
    await first.checks(3)
    ok_checks = first.loop.status.checks_total
    await first.stop()
    second = await make_stand(site=site)
    hours = await second.storage.stats.since(second.clock.now() - timedelta(hours=2))
    assert sum(h.checks for h in hours) == ok_checks
