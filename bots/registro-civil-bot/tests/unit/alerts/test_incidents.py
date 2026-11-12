"""Учёт инцидентов (SPEC 2.3, модель инцидента): открытие, изменение, закрытие, «Вижу»,
восстановление после перезапуска, привязка доставок к номеру."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.fakes import T0, TARGET, settle, slots
from tests.unit.alerts.helpers import appeared, changed, gone, make_rig

from rcbot.application.incidents import IncidentBook
from rcbot.domain.models import Channel, DeliveryResult, IncidentStatus


async def test_появление_открывает_инцидент_со_слотами_и_временем_снимка() -> None:
    rig = make_rig()
    assert await rig.book.current() is None
    await rig.book.on_appeared(appeared(23, 24))
    incident = await rig.book.current()
    assert incident is not None and incident.id == 1
    assert incident.slots == slots(23, 24)
    assert incident.opened_at == T0
    assert incident.target_id == TARGET.id
    assert incident.status is IncidentStatus.OPEN and not incident.acked
    assert rig.repo.incidents[1] is incident


async def test_изменение_обновляет_слоты_в_бд_и_в_объекте_не_создавая_нового_инцидента() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.book.on_changed(changed(23, 24))
    assert len(rig.repo.incidents) == 1
    assert rig.repo.incidents[1].slots == slots(23, 24)
    current = await rig.book.current()
    assert current is not None and current.slots == slots(23, 24)


async def test_изменение_без_открытого_инцидента_ничего_не_делает() -> None:
    rig = make_rig()
    await rig.book.on_changed(changed(23, 24))
    assert rig.repo.incidents == {}
    assert await rig.book.current() is None


async def test_пропажа_закрывает_инцидент_и_обнуляет_текущий() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    moment = T0 + timedelta(minutes=7)
    await rig.book.on_gone(gone(moment))
    assert await rig.book.current() is None
    closed = rig.repo.incidents[1]
    assert closed.status is IncidentStatus.CLOSED
    assert closed.closed_at == moment


async def test_пропажа_без_инцидента_безопасна() -> None:
    rig = make_rig()
    await rig.book.on_gone(gone())
    assert rig.repo.incidents == {}


async def test_два_быстрых_события_пропажи_закрывают_инцидент_один_раз() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    first, second = T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    await asyncio.gather(rig.book.on_gone(gone(first)), rig.book.on_gone(gone(second)))
    assert rig.repo.incidents[1].closed_at == first
    assert await rig.book.current() is None


async def test_пропажа_пока_инцидент_ещё_открывается_ждёт_номер_и_закрывает_его() -> None:
    rig = make_rig()
    release = asyncio.Event()
    rig.repo.open_delay = release.wait
    opening = asyncio.create_task(rig.book.on_appeared(appeared(23)))
    await settle()
    closing = asyncio.create_task(rig.book.on_gone(gone(T0 + timedelta(seconds=5))))
    await settle()
    assert not closing.done()
    release.set()
    await asyncio.gather(opening, closing)
    assert rig.repo.incidents[1].status is IncidentStatus.CLOSED
    assert await rig.book.current() is None


async def test_новый_инцидент_после_закрытия_получает_новый_номер() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.book.on_gone(gone())
    await rig.book.on_appeared(appeared(25))
    incident = await rig.book.current()
    assert incident is not None and incident.id == 2
    assert rig.repo.incidents[1].status is IncidentStatus.CLOSED


# --- «Вижу» ---


async def test_подтверждение_фиксирует_момент_и_не_закрывает_инцидент() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.clock.advance(42)
    incident = await rig.book.ack()
    assert incident is not None
    assert incident.acked_at == T0 + timedelta(seconds=42)
    assert rig.repo.incidents[1].acked_at == incident.acked_at
    assert rig.repo.incidents[1].status is IncidentStatus.OPEN
    assert (await rig.book.current()) is incident


async def test_повторное_подтверждение_не_меняет_время() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.clock.advance(10)
    await rig.book.ack()
    first = rig.repo.incidents[1].acked_at
    await rig.clock.advance(1000)
    again = await rig.book.ack()
    assert again is not None and again.acked_at == first
    assert rig.repo.incidents[1].acked_at == first


async def test_подтверждение_без_инцидента_возвращает_none() -> None:
    rig = make_rig()
    assert await rig.book.ack() is None


async def test_после_подтверждения_изменение_слотов_инцидент_не_сбрасывает_подтверждение() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.book.ack()
    await rig.book.on_changed(changed(23, 24))
    current = await rig.book.current()
    assert (
        current is not None and current.acked
    )  # «Вижу» относится к инциденту, а не к набору слотов


# --- восстановление ---


async def test_restore_продолжает_открытый_инцидент_после_перезапуска() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    # «Перезапуск»: новая книга на той же БД
    book = IncidentBook(rig.repo, rig.clock, TARGET.id)
    assert await book.current() is None
    restored = await book.restore()
    assert restored is not None and restored.id == 1
    assert (await book.current()) is restored
    await book.on_changed(changed(23, 24))
    assert rig.repo.incidents[1].slots == slots(23, 24)
    await book.ack()
    assert rig.repo.incidents[1].acked_at is not None
    await book.on_gone(gone())
    assert rig.repo.incidents[1].status is IncidentStatus.CLOSED
    assert len(rig.repo.incidents) == 1  # новый инцидент не заводился


async def test_restore_без_открытого_инцидента_ничего_не_восстанавливает() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.book.on_gone(gone())
    book = IncidentBook(rig.repo, rig.clock, TARGET.id)
    assert await book.restore() is None
    assert await book.current() is None


async def test_restore_не_подхватывает_инцидент_чужой_цели() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    other = IncidentBook(rig.repo, rig.clock, TARGET.id + 1)
    assert await other.restore() is None


# --- доставки ---


def _result(attempt: int = 1, incident_id: int | None = None) -> DeliveryResult:
    return DeliveryResult(Channel.NTFY, True, 12.0, attempt, "", incident_id)


async def test_доставка_получает_номер_текущего_инцидента_и_момент_записи() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.clock.advance(5)
    await rig.book.record(_result(attempt=2))
    [(saved, at)] = rig.repo.deliveries
    assert saved.incident_id == 1
    assert (saved.channel, saved.ok, saved.attempt, saved.latency_ms) == (
        Channel.NTFY,
        True,
        2,
        12.0,
    )
    assert at == T0 + timedelta(seconds=5)


async def test_доставка_с_уже_известным_номером_не_переписывается() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.book.record(_result(incident_id=77))
    assert rig.repo.deliveries[0][0].incident_id == 77


async def test_доставка_ждёт_номер_которого_БД_ещё_не_выдала() -> None:
    rig = make_rig()
    release = asyncio.Event()
    rig.repo.open_delay = release.wait
    opening = asyncio.create_task(rig.book.on_appeared(appeared(23)))
    await settle()
    recording = asyncio.create_task(rig.book.record(_result()))
    await settle()
    assert rig.repo.deliveries == []  # записать с пустым номером нельзя — ждём
    release.set()
    await asyncio.gather(opening, recording)
    assert [r.incident_id for r, _ in rig.repo.deliveries] == [1]


async def test_доставка_без_инцидента_пишется_без_номера_и_не_падает() -> None:
    rig = make_rig()
    await rig.book.record(_result())
    assert [r.incident_id for r, _ in rig.repo.deliveries] == [None]


# --- сбой БД ---


async def test_сбой_открытия_в_бд_не_отравляет_книгу_навсегда() -> None:
    """Если БД отказала при открытии, книга не должна бросать исключение на каждом обращении:
    ни `/status`, ни «Вижу», ни закрытие, ни следующий инцидент."""
    rig = make_rig()
    failures = [OSError("диск полон")]

    async def flaky() -> None:
        if failures:
            raise failures.pop()

    rig.repo.open_delay = flaky
    with pytest.raises(OSError):
        await rig.book.on_appeared(appeared(23))
    assert await rig.book.current() is None
    assert await rig.book.ack() is None
    await rig.book.record(_result())
    await rig.book.on_gone(gone())
    await rig.book.on_appeared(appeared(24))
    incident = await rig.book.current()
    assert incident is not None and incident.slots == slots(24)
