"""Первая волна тревоги (SPEC 2.4, раздел 5): параллельность, таймауты, изоляция сбоев каналов,
служебные алерты только в Telegram, запись доставок в БД."""

from __future__ import annotations

import asyncio
import itertools
import time

import pytest
from tests.fakes import T0, ManualClock, RecordingJournal, settle, slots, snapshot
from tests.unit.alerts.helpers import (
    Rig,
    appeared,
    changed,
    gone,
    hang,
    make_rig,
)

from rcbot.application import alerting
from rcbot.application.alerting import RepeatPolicy
from rcbot.application.bus import EventBus
from rcbot.domain.events import (
    ManualActionRequired,
    PollerStateChanged,
    SlotsAppeared,
    SourceSwitched,
    UnconfirmedSlotsSeen,
)
from rcbot.domain.models import (
    AlertAction,
    AlertKind,
    Channel,
    PollState,
    SourceKind,
)

NO_REPEATS: dict[Channel, RepeatPolicy] = {}


# --- параллельность и таймауты ---


async def test_первая_волна_уходит_во_все_три_канала_с_номером_попытки_1() -> None:
    rig = make_rig(policies=NO_REPEATS)
    await rig.alerts.on_appeared(appeared(23, 24))
    for channel in Channel:
        [(alert, attempt)] = rig.sent(channel)
        assert attempt == 1
        assert alert.kind is AlertKind.APPEARED
        assert alert.action is AlertAction.ACK
    # В ntfy и звонок личные данные не уходят: только даты и общая фраза (SPEC 2.4)
    [(tg, _)] = rig.sent(Channel.TELEGRAM)
    assert "TEST-1" in tg.text
    [(call, _)] = rig.sent(Channel.CALL)
    assert call.call_text and "TEST-1" not in call.push_text + call.call_text


async def test_медленный_канал_не_задерживает_остальные() -> None:
    clock = ManualClock()
    rig = make_rig(
        policies=NO_REPEATS,
        clock=clock,
        notifier_kwargs={Channel.TELEGRAM: {"delay": hang(clock, 8)}},
    )
    task = asyncio.create_task(rig.alerts.on_appeared(appeared(23)))
    await settle()
    # Быстрые каналы уже отправили в момент 0, медленный ещё «в пути»
    assert rig.notifiers[Channel.NTFY].times == [0.0]
    assert rig.notifiers[Channel.CALL].times == [0.0]
    assert rig.sent(Channel.TELEGRAM) == []
    assert not task.done()
    await clock.advance(8)
    await task
    assert rig.notifiers[Channel.TELEGRAM].times == [8.0]


async def test_каналы_отправляют_параллельно_а_не_по_очереди() -> None:
    clock = ManualClock()
    delay = {"delay": hang(clock, 5)}
    rig = make_rig(policies=NO_REPEATS, clock=clock, notifier_kwargs={c: delay for c in Channel})
    task = asyncio.create_task(rig.alerts.on_appeared(appeared(23)))
    await settle()
    await clock.advance(5)  # один шаг в 5 с завершает все три; последовательно нужно было бы 15 с
    assert task.done()
    assert [n.times for n in rig.notifiers.values()] == [[5.0]] * 3


async def test_у_каждого_канала_свой_таймаут_и_результат_таймаут(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(alerting, "SEND_TIMEOUT_SEC", 0.05)
    never = asyncio.Event()
    rig = make_rig(
        policies=NO_REPEATS,
        notifier_kwargs={
            Channel.NTFY: {"delay": never.wait},
            Channel.CALL: {"delay": never.wait},
        },
    )
    started = time.monotonic()
    results = await rig.alerts.test()
    assert time.monotonic() - started < 1.0  # таймауты идут одновременно, а не друг за другом
    by_channel = {r.channel: r for r in results}
    assert by_channel[Channel.TELEGRAM].ok
    for slow in (Channel.NTFY, Channel.CALL):
        assert not by_channel[slow].ok
        assert by_channel[slow].error == "таймаут"
        assert by_channel[slow].latency_ms == 50.0
        assert by_channel[slow].attempt == 1


async def test_таймаут_канала_по_умолчанию_12_секунд() -> None:
    assert alerting.SEND_TIMEOUT_SEC == 12.0


async def test_исключение_канала_не_ломает_рассылку_по_остальным() -> None:
    rig = make_rig(
        policies=NO_REPEATS,
        notifier_kwargs={Channel.NTFY: {"raises": RuntimeError("сеть упала")}},
    )
    await rig.alerts.on_appeared(appeared(23))
    assert rig.attempts(Channel.TELEGRAM) == [1]
    assert rig.attempts(Channel.CALL) == [1]
    [failed] = [r for r in rig.delivered if r.channel is Channel.NTFY]
    assert not failed.ok
    assert failed.error == "RuntimeError"
    assert (
        "сеть упала" not in failed.error
    )  # текст исключения (возможен секрет) в результат не идёт


async def test_отказ_канала_ok_false_попадает_в_результат_и_журнал() -> None:
    rig = make_rig(
        policies=NO_REPEATS,
        notifier_kwargs={Channel.CALL: {"ok": False, "error": "лимит"}},
    )
    results = await rig.alerts.test()
    assert {r.channel: r.ok for r in results} == {
        Channel.TELEGRAM: True,
        Channel.NTFY: True,
        Channel.CALL: False,
    }
    deliveries = [e for e in rig.journal.events if e[0] == "delivery"]
    assert len(deliveries) == 3
    [(_, message, level, data)] = [e for e in deliveries if e[3]["channel"] == "call"]
    assert level == "WARNING" and "не доставлено: лимит" in message and data["ok"] is False
    assert all(e[2] == "INFO" for e in deliveries if e[3]["ok"])


async def test_исключение_слушателя_доставки_не_роняет_рассылку_и_повторы() -> None:
    rig = make_rig()

    def broken(_: object) -> None:
        raise RuntimeError("слушатель сломан")

    rig.alerts.delivery_listeners.insert(0, broken)
    await rig.alerts.on_appeared(appeared(23))
    # Все три канала получили тревогу, повторы запущены, остальные слушатели вызваны
    assert [n.attempts for n in rig.notifiers.values()] == [[1], [1], [1]]
    assert len(rig.delivered) == 3
    assert rig.alerts.repeating()
    await rig.clock.advance(60)
    assert rig.attempts(Channel.TELEGRAM) == [1, 2]
    await rig.alerts.close()


async def test_сбой_волны_не_лишает_тревогу_повторов() -> None:
    class BrokenJournal(RecordingJournal):
        def event(self, code: str, message: str, level: str = "INFO", **data: object) -> None:
            if code.startswith("alert."):
                raise OSError("диск недоступен")
            super().event(code, message, level, **data)

    rig = make_rig()
    rig.alerts._journal = BrokenJournal()
    await rig.alerts.on_appeared(appeared(23))
    # Первая отправка не состоялась, но тревога не должна замолчать совсем: повторы идут
    assert rig.alerts.repeating()
    await rig.clock.advance(60)
    assert rig.attempts(Channel.TELEGRAM) == [2]
    await rig.alerts.close()


# --- какие каналы участвуют ---


async def test_выключенный_канал_не_получает_волну_но_тест_идёт_во_все() -> None:
    rig = make_rig(policies=NO_REPEATS)
    rig.runtime.channels_off.add(Channel.NTFY)
    await rig.alerts.on_appeared(appeared(23))
    assert rig.attempts(Channel.NTFY) == []
    assert rig.attempts(Channel.TELEGRAM) == [1]
    results = await rig.alerts.test()
    assert {r.channel for r in results} == set(Channel)  # проверка канала работает и у выключенных


async def test_если_все_каналы_выключены_рассылки_нет_и_ошибки_нет(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig(policies=NO_REPEATS)
    rig.runtime.channels_off.update(Channel)
    await rig.alerts.on_appeared(appeared(23))
    assert all(not n.sent for n in rig.notifiers.values())
    assert "нет включённых каналов" in caplog.text
    assert "alert.appeared" not in rig.journal.codes()


async def test_канала_нет_среди_настроенных_пропускается_без_ошибки_и_без_повторов() -> None:
    rig = make_rig(channels={Channel.TELEGRAM})
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(60 * 31)
    assert rig.attempts(Channel.TELEGRAM) == list(range(1, 32))
    results = await rig.alerts.test({Channel.NTFY, Channel.TELEGRAM})
    assert [r.channel for r in results] == [Channel.TELEGRAM]


async def test_тест_одного_канала_шлёт_только_в_него_и_не_повторяется() -> None:
    rig = make_rig()
    results = await rig.alerts.test({Channel.CALL})
    assert [r.channel for r in results] == [Channel.CALL]
    assert rig.sent(Channel.TELEGRAM) == []
    assert not rig.alerts.repeating()
    [(alert, _)] = rig.sent(Channel.CALL)
    assert alert.kind is AlertKind.TEST


async def test_политика_без_повторов_или_отсутствует_повторов_не_создаёт() -> None:
    rig = make_rig(
        policies={Channel.TELEGRAM: RepeatPolicy(60.0, 0), Channel.NTFY: RepeatPolicy(120.0, 3)}
    )
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(10_000)
    assert rig.attempts(Channel.TELEGRAM) == [1]
    assert rig.attempts(Channel.NTFY) == [1, 2, 3, 4]
    assert rig.attempts(Channel.CALL) == [1]  # политики нет — одна отправка


# --- служебные алерты: только Telegram ---


def _only_telegram(rig: Rig) -> list[str]:
    assert rig.attempts(Channel.NTFY) == [] and rig.attempts(Channel.CALL) == []
    return [alert.text for alert, _ in rig.sent(Channel.TELEGRAM)]


async def test_пропажа_мест_идёт_только_в_telegram() -> None:
    rig = make_rig()
    await rig.alerts.on_gone(gone())
    [text] = _only_telegram(rig)
    assert "Места пропали" in text
    assert not rig.alerts.repeating()


async def test_ручной_шаг_идёт_только_в_telegram_с_кнопкой_готово() -> None:
    rig = make_rig()
    await rig.alerts.on_manual(ManualActionRequired(T0, "контрольный вопрос"))
    [(alert, attempt)] = rig.sent(Channel.TELEGRAM)
    assert alert.action is AlertAction.MANUAL_DONE and attempt == 1
    assert "контрольный вопрос" in alert.text
    _only_telegram(rig)
    await rig.clock.advance(3600)
    assert len(rig.sent(Channel.TELEGRAM)) == 1  # служебное сообщение не повторяется


async def test_смена_источника_идёт_только_в_telegram() -> None:
    rig = make_rig()
    await rig.alerts.on_source_switched(
        SourceSwitched(T0, SourceKind.API, SourceKind.DOM, "3 неожиданных ответа")
    )
    [text] = _only_telegram(rig)
    assert "api → dom" in text and "3 неожиданных ответа" in text


async def test_неподтверждённые_места_sae_идут_только_в_telegram_без_кнопки() -> None:
    rig = make_rig()
    event = UnconfirmedSlotsSeen(T0, snapshot(slots(5), source=SourceKind.SAE))
    await rig.alerts.on_unconfirmed(event)
    [(alert, _)] = rig.sent(Channel.TELEGRAM)
    assert alert.kind is AlertKind.UNCONFIRMED and alert.action is AlertAction.NONE
    _only_telegram(rig)


async def test_произвольное_служебное_сообщение_идёт_только_в_telegram() -> None:
    rig = make_rig()
    await rig.alerts.service("бот запущен")
    assert _only_telegram(rig) == ["бот запущен"]


_SLOW = PollState.BACKOFF
_COMBOS = [(o, n) for o, n in itertools.product(PollState, PollState) if o is not n]


@pytest.mark.parametrize(("old", "new"), _COMBOS)
async def test_смена_состояния_сообщается_только_для_замедления_и_возврата(
    old: PollState, new: PollState
) -> None:
    rig = make_rig()
    await rig.alerts.on_state_changed(PollerStateChanged(T0, old, new, "429 от сайта"))
    expect_slow = new is PollState.BACKOFF
    expect_back = new is PollState.RUNNING and old in (PollState.BACKOFF, PollState.NEEDS_MANUAL)
    texts = [] if not (expect_slow or expect_back) else _only_telegram(rig)
    if not (expect_slow or expect_back):
        assert all(not n.sent for n in rig.notifiers.values())
    elif expect_slow:
        assert "замедлился" in texts[0] and "429 от сайта" in texts[0]
    else:
        assert "снова работает" in texts[0]


# --- ожидаемые побочные эффекты тревоги ---


async def test_после_тревоги_бот_10_минут_не_входит_на_сайт_D5() -> None:
    rig = make_rig(policies=NO_REPEATS)
    assert rig.session.quiet_remaining() == 0
    await rig.alerts.on_appeared(appeared(23))
    assert rig.session.quiet_remaining() == 600.0
    await rig.clock.advance(250)
    assert rig.session.quiet_remaining() == 350.0


async def test_новая_тревога_об_изменении_тоже_продлевает_тишину() -> None:
    rig = make_rig(policies=NO_REPEATS)
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(300)
    await rig.alerts.on_changed(changed(23, 24))
    assert rig.session.quiet_remaining() == 600.0


async def test_служебные_сообщения_и_тест_тишину_не_включают() -> None:
    rig = make_rig(policies=NO_REPEATS)
    await rig.alerts.on_gone(gone())
    await rig.alerts.on_manual(ManualActionRequired(T0, "x"))
    await rig.alerts.service("привет")
    await rig.alerts.test()
    assert rig.session.quiet_remaining() == 0


async def test_координатор_отказал_тревоги_нет_и_тишина_не_включается() -> None:
    class Refuse:
        def __init__(self) -> None:
            self.seen: list[str] = []

        async def should_alert(self, fingerprint: str) -> bool:
            self.seen.append(fingerprint)
            return False

    coordinator = Refuse()
    rig = make_rig(coordinator=coordinator)
    event = appeared(23)
    await rig.alerts.on_appeared(event)
    assert coordinator.seen == [event.snapshot.fingerprint]
    assert all(not n.sent for n in rig.notifiers.values())
    assert not rig.alerts.repeating()
    assert rig.session.quiet_remaining() == 0


async def test_сообщение_об_изменении_содержит_новые_даты_и_идёт_всем_каналам() -> None:
    rig = make_rig(policies=NO_REPEATS)
    await rig.alerts.on_changed(changed(23, 24, added=slots(24)))
    for channel in Channel:
        [(alert, attempt)] = rig.sent(channel)
        assert alert.kind is AlertKind.CHANGED and attempt == 1
    assert "Новые: 24.02" in rig.sent(Channel.TELEGRAM)[0][0].text


# --- запись доставок в БД ---


async def test_доставки_появления_и_изменения_пишутся_в_бд_с_номером_инцидента() -> None:
    rig = make_rig(policies=NO_REPEATS)
    await rig.book.on_appeared(appeared(23))
    await rig.alerts.on_appeared(appeared(23))
    await rig.alerts._background.drain()  # дожидается фоновых записей, рассылка остаётся живой
    recorded = await rig.repo.deliveries_for(1)
    assert {r.channel for r in recorded} == set(Channel)
    assert all(r.attempt == 1 for r in recorded)
    await rig.alerts.on_changed(changed(23, 24))
    await rig.alerts._background.drain()
    assert len(await rig.repo.deliveries_for(1)) == 6


async def test_после_close_новые_тревоги_не_отправляются_и_повторы_не_создаются() -> None:
    rig = make_rig()
    await rig.alerts.close()
    await rig.alerts.on_appeared(appeared(23))
    await rig.alerts.service("после остановки")
    assert all(not n.sent for n in rig.notifiers.values())
    assert not rig.alerts.repeating()


async def test_close_дожидается_отменённых_повторов() -> None:
    rig = make_rig()
    await rig.alerts.on_appeared(appeared(23))
    assert rig.alerts.repeating()
    repeats = list(rig.alerts._repeats)
    await rig.alerts.close()
    assert repeats and all(t.done() for t in repeats)


async def test_служебные_тестовые_и_имитированные_тревоги_в_бд_не_пишутся() -> None:
    rig = make_rig(policies=NO_REPEATS)
    await rig.book.on_appeared(appeared(23))
    await rig.alerts.on_gone(gone())
    await rig.alerts.on_manual(ManualActionRequired(T0, "x"))
    await rig.alerts.service("x")
    await rig.alerts.test()
    await rig.alerts.close()
    assert rig.repo.deliveries == []


async def test_доставка_до_выдачи_номера_инцидента_получает_правильный_номер() -> None:
    """Гонка SPEC 2.2 п. 4: тревога уходит раньше БД, а доставка пишется после номера."""
    rig = make_rig(policies=NO_REPEATS)
    release = asyncio.Event()
    rig.repo.open_delay = release.wait
    bus = EventBus()
    bus.subscribe(SlotsAppeared, rig.alerts.on_appeared)
    bus.subscribe(SlotsAppeared, rig.book.on_appeared)
    bus.publish(appeared(23))
    await settle()
    # Уведомления уже ушли, БД ещё не ответила, доставки ждут номера
    assert all(n.attempts == [1] for n in rig.notifiers.values())
    assert rig.repo.incidents == {} and rig.repo.deliveries == []
    release.set()
    await settle()
    await bus.drain()
    await rig.alerts.close()
    assert sorted(r.incident_id for r, _ in rig.repo.deliveries if r.incident_id) == [1, 1, 1]
    assert len(rig.repo.deliveries) == 3


async def test_доставки_повторов_тоже_пишутся_в_бд() -> None:
    rig = make_rig(policies={Channel.TELEGRAM: RepeatPolicy(60.0, 2)})
    await rig.book.on_appeared(appeared(23))
    await rig.alerts.on_appeared(appeared(23))
    await rig.clock.advance(120)
    await rig.alerts.close()
    telegram = [r for r, _ in rig.repo.deliveries if r.channel is Channel.TELEGRAM]
    assert [r.attempt for r in telegram] == [1, 2, 3]
    assert {r.incident_id for r in telegram} == {1}


async def test_слушатели_доставок_видят_каждый_результат_включая_отказы() -> None:
    rig = make_rig(
        policies=NO_REPEATS, notifier_kwargs={Channel.NTFY: {"ok": False, "error": "502"}}
    )
    await rig.alerts.on_appeared(appeared(23))
    assert sorted((r.channel.value, r.ok) for r in rig.delivered) == [
        ("call", True),
        ("ntfy", False),
        ("telegram", True),
    ]


async def test_уведомление_об_успешной_отправке_имеет_задержку_канала() -> None:
    rig = make_rig(policies=NO_REPEATS, notifier_kwargs={Channel.TELEGRAM: {"latency_ms": 42.0}})
    await rig.alerts.on_appeared(appeared(23))
    [tg] = [r for r in rig.delivered if r.channel is Channel.TELEGRAM]
    assert tg.latency_ms == 42.0


async def test_возврат_через_паузу_после_замедления_тоже_сообщается() -> None:
    # BACKOFF → PAUSED → RUNNING раньше молчал: «снова работает» требовало old == BACKOFF
    rig = make_rig()
    for old, new in [
        (PollState.RUNNING, PollState.BACKOFF),
        (PollState.BACKOFF, PollState.PAUSED),
        (PollState.PAUSED, PollState.RUNNING),
    ]:
        await rig.alerts.on_state_changed(PollerStateChanged(T0, old, new, "429"))
    texts = _only_telegram(rig)
    assert len(texts) == 2
    assert "замедлился" in texts[0] and "снова работает" in texts[1]


async def test_снова_работает_сообщается_один_раз_на_одну_деградацию() -> None:
    rig = make_rig()
    await rig.alerts.on_state_changed(
        PollerStateChanged(T0, PollState.RUNNING, PollState.BACKOFF, "x")
    )
    await rig.alerts.on_state_changed(
        PollerStateChanged(T0, PollState.BACKOFF, PollState.RUNNING, "x")
    )
    await rig.alerts.on_state_changed(
        PollerStateChanged(T0, PollState.PAUSED, PollState.RUNNING, "x")
    )
    assert len(_only_telegram(rig)) == 2
