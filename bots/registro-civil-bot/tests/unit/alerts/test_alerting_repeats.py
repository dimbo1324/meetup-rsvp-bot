"""Повторы тревоги (SPEC 2.4, раздел 5): расписание, «Вижу», пропажа мест, новая кампания,
выключенные каналы, прогрев, закрытие, simulate."""

from __future__ import annotations

import asyncio

import pytest
from tests.fakes import ManualClock, settle
from tests.unit.alerts.helpers import appeared, changed, gone, hang, make_rig

from rcbot.application import alerting
from rcbot.domain.events import PollerStateChanged
from rcbot.domain.models import AlertKind, Channel, IncidentStatus, PollState

# Канал → (период, число повторов) из SPEC 2.4
SCHEDULE = {
    Channel.TELEGRAM: (60, 30),
    Channel.NTFY: (120, 10),
    Channel.CALL: (180, 5),
}


# --- расписание ---


@pytest.mark.parametrize("channel", list(Channel))
async def test_повторы_идут_строго_по_расписанию_spec_и_после_последнего_отправок_нет(
    channel: Channel,
) -> None:
    every, count = SCHEDULE[channel]
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(every * count * 3)
    notifier = rig.notifiers[channel]
    # Первая отправка + count повторов с номерами попыток 2…count+1 в моменты every, 2·every, …
    assert notifier.attempts == list(range(1, count + 2))
    assert notifier.times == [float(every * i) for i in range(count + 1)]
    assert not rig.alerts.repeating()


async def test_повтор_не_приходит_раньше_срока_ни_на_секунду() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(59)
    assert rig.attempts(Channel.TELEGRAM) == [1]
    await rig.clock.advance(1)
    assert rig.attempts(Channel.TELEGRAM) == [1, 2]
    await rig.clock.advance(60)
    assert rig.attempts(Channel.TELEGRAM) == [1, 2, 3]
    # ntfy (120 с) и звонок (180 с) идут своими таймерами
    assert rig.attempts(Channel.NTFY) == [1, 2]
    assert rig.attempts(Channel.CALL) == [1]
    await rig.clock.advance(60)
    assert rig.attempts(Channel.CALL) == [1, 2]


async def test_повторы_приходят_тем_же_текстом_что_и_первая_отправка() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23, 24))
    await rig.clock.advance(60)
    first, repeat = (alert for alert, _ in rig.sent(Channel.TELEGRAM))
    assert first == repeat
    assert first.kind is AlertKind.APPEARED


async def test_отказавший_канал_повторяется_по_тому_же_расписанию() -> None:
    rig = make_rig(notifier_kwargs={Channel.NTFY: {"ok": False, "error": "502"}})
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(360)
    assert rig.attempts(Channel.NTFY) == [1, 2, 3, 4]


async def test_падающий_канал_не_прерывает_серию_повторов() -> None:
    rig = make_rig(notifier_kwargs={Channel.CALL: {"raises": ConnectionError("нет сети")}})
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(180 * 6)
    failures = [r for r in rig.delivered if r.channel is Channel.CALL]
    assert [r.attempt for r in failures] == [1, 2, 3, 4, 5, 6]
    assert all(r.error == "ConnectionError" for r in failures)
    assert rig.attempts(Channel.TELEGRAM)[:3] == [1, 2, 3]


async def test_повтор_после_таймаута_канала_продолжается(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(alerting, "SEND_TIMEOUT_SEC", 0.02)
    never = asyncio.Event()
    rig = make_rig(notifier_kwargs={Channel.TELEGRAM: {"delay": never.wait}})
    await rig.alerts.on_appeared(appeared(23))
    for expected in (2, 3):
        await rig.clock.advance(60)
        for _ in range(200):  # таймаут настоящий (20 мс), а не ручных часов
            if len([r for r in rig.delivered if r.channel is Channel.TELEGRAM]) == expected:
                break
            await asyncio.sleep(0.01)
    timeouts = [r for r in rig.delivered if r.channel is Channel.TELEGRAM]
    assert [(r.attempt, r.error) for r in timeouts] == [
        (1, "таймаут"),
        (2, "таймаут"),
        (3, "таймаут"),
    ]


# --- «Вижу» ---


async def test_вижу_останавливает_повторы_во_всех_каналах_инцидент_остаётся_открытым() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(125)  # telegram: 1,2,3; ntfy: 1,2
    before = {c: list(n.attempts) for c, n in rig.notifiers.items()}
    assert rig.alerts.repeating()
    assert await rig.alerts.ack() is True
    assert not rig.alerts.repeating()
    await rig.clock.advance(20_000)
    assert {c: n.attempts for c, n in rig.notifiers.items()} == before
    incident = await rig.book.current()
    assert incident is not None
    assert incident.status is IncidentStatus.OPEN
    assert incident.closed_at is None
    assert incident.acked_at == rig.repo.incidents[1].acked_at is not None
    assert "alert.ack" in rig.journal.codes()


async def test_вижу_идемпотентно_время_подтверждения_не_меняется() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(30)
    await rig.alerts.ack()
    first = rig.repo.incidents[1].acked_at
    await rig.clock.advance(500)
    assert await rig.alerts.ack() is True  # открытый инцидент есть: «Вижу» не отвергается
    assert rig.repo.incidents[1].acked_at == first


async def test_вижу_без_инцидента_и_без_повторов_возвращает_false() -> None:
    rig = make_rig()
    assert await rig.alerts.ack() is False
    assert "alert.ack" in rig.journal.codes()  # попытка подтверждения всё равно в журнале


async def test_вижу_при_закончившихся_повторах_подтверждает_открытый_инцидент() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(5000)
    assert not rig.alerts.repeating()
    assert await rig.alerts.ack() is True
    assert rig.repo.incidents[1].acked_at is not None


async def test_вижу_для_имитации_без_инцидента_тоже_останавливает_повторы() -> None:
    rig = make_rig()
    await rig.alerts.simulate()
    assert rig.alerts.repeating()
    assert await rig.alerts.ack() is True
    await rig.clock.advance(10_000)
    assert all(n.attempts == [1] for n in rig.notifiers.values())


# --- пропажа мест и новая кампания ---


async def test_пропажа_мест_останавливает_повторы_закрывает_инцидент_и_пишет_в_telegram() -> None:
    rig = make_rig()
    await rig.book.on_appeared(appeared(23))
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(65)
    event = gone()
    await rig.alerts.on_gone(event)
    await rig.book.on_gone(event)
    assert not rig.alerts.repeating()
    await rig.clock.advance(20_000)
    # Telegram: тревога, повтор и одно сообщение «места пропали»; остальные каналы — только первая
    assert rig.attempts(Channel.TELEGRAM) == [1, 2, 1]
    assert rig.attempts(Channel.NTFY) == [1]
    assert rig.attempts(Channel.CALL) == [1]
    assert rig.repo.incidents[1].status is IncidentStatus.CLOSED
    assert await rig.book.current() is None


async def test_изменение_отменяет_прежние_повторы_и_начинает_кампанию_заново() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(150)  # telegram: 1,2,3 (60, 120); ntfy: 1,2 (120); звонок: 1
    await rig.alerts.on_changed(changed(23, 24))
    started = rig.clock.now()
    for channel in Channel:
        assert rig.attempts(channel)[-1] == 1  # новая волна с попытки 1
    await rig.clock.advance(60)
    # Ровно одна серия на канал: не удвоенные повторы (прежний таймер на 180-й секунде мёртв)
    assert rig.attempts(Channel.TELEGRAM) == [1, 2, 3, 1, 2]
    await rig.clock.advance(60)
    assert rig.attempts(Channel.TELEGRAM) == [1, 2, 3, 1, 2, 3]
    assert rig.attempts(Channel.NTFY) == [1, 2, 1, 2]
    alerts_after = [a.kind for a, att in rig.sent(Channel.TELEGRAM)][3:]
    assert alerts_after == [AlertKind.CHANGED] * 3
    assert (rig.clock.now() - started).total_seconds() == 120


async def test_изменение_после_подтверждения_снова_запускает_повторы() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    await rig.alerts.ack()
    assert not rig.alerts.repeating()
    await rig.alerts.on_changed(changed(23, 24))
    assert rig.alerts.repeating()
    await rig.clock.advance(60)
    assert rig.attempts(Channel.TELEGRAM) == [1, 1, 2]


async def test_новое_появление_после_пропажи_запускает_свою_серию_с_нуля() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    await rig.alerts.on_gone(gone())
    await rig.alerts.on_appeared(appeared(25))
    await rig.clock.advance(60)
    assert rig.attempts(Channel.TELEGRAM) == [1, 1, 1, 2]


# --- выключение каналов в середине серии ---


async def test_канал_выключенный_в_середине_серии_перестаёт_получать_повторы() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(120)
    assert rig.attempts(Channel.TELEGRAM) == [1, 2, 3]
    rig.runtime.channels_off.add(Channel.TELEGRAM)
    await rig.clock.advance(600)
    assert rig.attempts(Channel.TELEGRAM) == [1, 2, 3]
    assert rig.attempts(Channel.NTFY) == [1, 2, 3, 4, 5, 6, 7]  # слоты 0…720 с, как шли


async def test_включённый_обратно_канал_возвращается_в_серию_без_превышения_лимита() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    rig.runtime.channels_off.add(Channel.CALL)
    await rig.clock.advance(200)  # слот 180 с пропущен
    rig.runtime.channels_off.discard(Channel.CALL)
    await rig.clock.advance(10_000)
    # Пропущенный слот считается использованным: после возвращения идут слоты 360, 540, …, 900
    assert rig.attempts(Channel.CALL) == [1, 3, 4, 5, 6]
    assert rig.notifiers[Channel.CALL].times[1:] == [360.0, 540.0, 720.0, 900.0]


async def test_выключенный_до_тревоги_канал_не_получает_ни_волну_ни_повторы() -> None:
    rig = make_rig()
    rig.runtime.channels_off.add(Channel.NTFY)
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(1500)
    assert rig.attempts(Channel.NTFY) == []
    assert rig.attempts(Channel.TELEGRAM)[-1] == 26


# --- закрытие ---


async def test_close_отменяет_повторы_и_ждёт_фоновые_записи() -> None:
    rig = make_rig()
    release = asyncio.Event()
    rig.repo.open_delay = release.wait
    opening = asyncio.create_task(rig.book.on_appeared(appeared(23)))
    await rig.alerts.on_appeared(appeared(23))  # запись доставок ждёт номер инцидента
    await settle()
    assert rig.repo.deliveries == []
    closing = asyncio.create_task(rig.alerts.close())
    await settle()
    assert not closing.done()  # фоновые записи ещё не завершены — close их дожидается
    assert not rig.alerts.repeating()  # но повторы уже отменены
    release.set()
    await closing
    await opening
    assert len(rig.repo.deliveries) == 3
    await rig.clock.advance(10_000)
    assert all(n.attempts == [1] for n in rig.notifiers.values())


async def test_close_без_работы_завершается_сразу_и_повторно() -> None:
    rig = make_rig()
    await rig.alerts.close()
    await rig.alerts.close()


# --- прогрев ---


async def test_прогрев_раз_в_240_секунд_во_все_каналы() -> None:
    rig = make_rig()
    task = asyncio.create_task(rig.alerts.keep_warm())
    await settle()
    assert [n.warmups for n in rig.notifiers.values()] == [1, 1, 1]
    await rig.clock.advance(239)
    assert [n.warmups for n in rig.notifiers.values()] == [1, 1, 1]
    await rig.clock.advance(1)
    assert [n.warmups for n in rig.notifiers.values()] == [2, 2, 2]
    await rig.clock.advance(480)
    assert [n.warmups for n in rig.notifiers.values()] == [4, 4, 4]
    assert rig.clock.sleeps.count(alerting.WARMUP_EVERY_SEC) >= 3
    await rig.alerts.close()
    await rig.clock.advance(240)
    await task  # после close() цикл прогрева выходит сам


async def test_ошибка_и_таймаут_прогрева_не_останавливают_цикл(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(alerting, "SEND_TIMEOUT_SEC", 0.02)
    rig = make_rig()

    async def broken() -> None:
        raise OSError("соединение сброшено")

    never = asyncio.Event()

    async def stuck() -> None:
        await never.wait()

    rig.notifiers[Channel.NTFY].warmup = broken  # type: ignore[method-assign]
    rig.notifiers[Channel.TELEGRAM].warmup = stuck  # type: ignore[method-assign]
    task = asyncio.create_task(rig.alerts.keep_warm())
    await rig.clock.advance(0)
    for _ in range(100):  # реальный таймаут 20 мс у зависшего прогрева
        if rig.clock.sleeping:
            break
        await asyncio.sleep(0.01)
    assert rig.notifiers[Channel.CALL].warmups == 1  # канал после сбойных всё равно прогрет
    await rig.clock.advance(240)
    for _ in range(100):
        if rig.notifiers[Channel.CALL].warmups == 2:
            break
        await asyncio.sleep(0.01)
    assert rig.notifiers[Channel.CALL].warmups == 2
    await rig.alerts.close()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- simulate ---


async def test_simulate_шлёт_тестовую_тревогу_с_повторами_но_без_записи_в_бд() -> None:
    rig = make_rig()
    await rig.alerts.simulate()
    await rig.clock.advance(360)
    await rig.alerts.close()
    for channel, count in ((Channel.TELEGRAM, 7), (Channel.NTFY, 4), (Channel.CALL, 3)):
        assert len(rig.sent(channel)) == count
        assert {alert.kind for alert, _ in rig.sent(channel)} == {AlertKind.TEST}
    assert rig.repo.deliveries == []


async def test_simulate_уважает_выключенные_каналы() -> None:
    rig = make_rig()
    rig.runtime.channels_off.add(Channel.CALL)
    await rig.alerts.simulate()
    await rig.clock.advance(180)
    assert rig.attempts(Channel.CALL) == []
    await rig.alerts.close()


# --- прочее ---


async def test_смена_состояния_во_время_повторов_их_не_трогает() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    await rig.alerts.on_state_changed(
        PollerStateChanged(rig.clock.now(), PollState.RUNNING, PollState.BACKOFF, "5xx")
    )
    assert rig.alerts.repeating()
    await rig.clock.advance(60)
    assert rig.attempts(Channel.TELEGRAM) == [1, 1, 2]  # тревога, служебное «замедлился», повтор


async def test_медленный_канал_первой_волны_не_сдвигает_повторы_остальных() -> None:
    clock = ManualClock()
    rig = make_rig(clock=clock, notifier_kwargs={Channel.CALL: {"delay": hang(clock, 10)}})
    task = asyncio.create_task(rig.alerts.on_appeared(appeared(23)))
    await settle()
    await clock.advance(10)
    await task
    await clock.advance(50)
    assert rig.notifiers[Channel.TELEGRAM].times == [0.0, 60.0]
    assert rig.notifiers[Channel.NTFY].times == [0.0]
    await clock.advance(60)
    assert rig.notifiers[Channel.NTFY].times == [0.0, 120.0]


async def test_вижу_во_время_первой_волны_не_даёт_повторам_стартовать_потом() -> None:
    """Звонок «висит» 10 с; владелец уже увидел сообщение в Telegram и жмёт «Вижу»."""
    clock = ManualClock()
    rig = make_rig(clock=clock, notifier_kwargs={Channel.CALL: {"delay": hang(clock, 10)}})
    await rig.book.on_appeared(appeared(23))
    task = asyncio.create_task(rig.alerts.on_appeared(appeared(23)))
    await settle()
    assert rig.attempts(Channel.TELEGRAM) == [1]
    assert await rig.alerts.ack() is True
    await clock.advance(10)
    await task
    await clock.advance(20_000)
    assert {c: n.attempts for c, n in rig.notifiers.items()} == {c: [1] for c in Channel}
    assert not rig.alerts.repeating()


async def test_пропажа_мест_во_время_первой_волны_не_даёт_повторам_стартовать_потом() -> None:
    clock = ManualClock()
    rig = make_rig(clock=clock, notifier_kwargs={Channel.CALL: {"delay": hang(clock, 10)}})
    task = asyncio.create_task(rig.alerts.on_appeared(appeared(23)))
    await settle()
    await rig.alerts.on_gone(gone())
    await clock.advance(10)
    await task
    await clock.advance(20_000)
    assert rig.attempts(Channel.NTFY) == [1]
    assert rig.attempts(Channel.TELEGRAM) == [1, 1]  # тревога и «места пропали», без повторов


async def test_две_тревоги_подряд_не_оставляют_две_серии_повторов() -> None:
    """Изменение приходит, пока первая волна ещё идёт: живой должна остаться одна серия."""
    clock = ManualClock()
    rig = make_rig(clock=clock, notifier_kwargs={Channel.CALL: {"delay": hang(clock, 10)}})
    first = asyncio.create_task(rig.alerts.on_appeared(appeared(23)))
    await settle()
    second = asyncio.create_task(rig.alerts.on_changed(changed(23, 24)))
    await settle()
    await clock.advance(10)
    await first
    await second
    await clock.advance(60)
    telegram = rig.sent(Channel.TELEGRAM)
    # Повтор получила только новая тревога (CHANGED); серия устаревшей отменена
    assert [(a.kind, n) for a, n in telegram] == [
        (AlertKind.APPEARED, 1),
        (AlertKind.CHANGED, 1),
        (AlertKind.CHANGED, 2),
    ]
