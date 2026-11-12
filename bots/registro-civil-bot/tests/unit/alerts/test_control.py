"""Фасад управления Controller (SPEC 2.5, критерий приёмки 9): команды владельца, статус, журнал."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.fakes import T0, FakeAuthenticator, settle, slots
from tests.unit.alerts.helpers import appeared, gone, make_control

from rcbot.application.control import FOREVER, IntervalTooSmall
from rcbot.application.runtime import K_CHANNELS_OFF, K_INTERVAL, K_MONTHS, K_PAUSED_UNTIL
from rcbot.config.settings import MAX_POLL_INTERVAL_SEC, MIN_POLL_INTERVAL_SEC
from rcbot.domain.errors import LoginFailed, ReloginBudgetExceeded
from rcbot.domain.models import AppEvent, Channel, DeliveryResult, PollState, SourceKind

# --- интервал: жёсткий минимум 10 с ---


def test_минимум_интервала_равен_10_секундам() -> None:
    assert MIN_POLL_INTERVAL_SEC == 10.0


@pytest.mark.parametrize("seconds", [9.99, 9.0, 5.0, 1.0, 0.0, -20.0])
async def test_интервал_меньше_10_секунд_отвергается_и_ничего_не_меняет(seconds: float) -> None:
    ctl = make_control()
    with pytest.raises(IntervalTooSmall):
        await ctl.controller.set_interval(seconds)
    assert ctl.rig.runtime.base_interval_sec == 20.0
    assert K_INTERVAL not in ctl.kv.data
    assert "command.interval" not in ctl.rig.journal.codes()


async def test_ровно_10_секунд_допустимо_и_сохраняется() -> None:
    ctl = make_control()
    assert await ctl.controller.set_interval(10.0) == 10.0
    assert ctl.rig.runtime.base_interval_sec == 10.0
    assert ctl.kv.data[K_INTERVAL] == "10"
    assert "command.interval" in ctl.rig.journal.codes()


async def test_слишком_большой_интервал_обрезается_до_максимума() -> None:
    ctl = make_control()
    assert await ctl.controller.set_interval(10_000.0) == MAX_POLL_INTERVAL_SEC


@given(
    seconds=st.one_of(
        st.floats(min_value=9.0, max_value=10.0, exclude_max=True),  # вплотную к границе
        st.floats(max_value=9.0, allow_nan=False, allow_infinity=False),
    )
)
async def test_свойство_ни_одно_значение_ниже_минимума_не_проходит(seconds: float) -> None:
    ctl = make_control()
    with pytest.raises(IntervalTooSmall):
        await ctl.controller.set_interval(seconds)
    assert ctl.rig.runtime.base_interval_sec == 20.0


@given(seconds=st.floats(min_value=10.0, max_value=1e7, allow_nan=False))
async def test_свойство_принятый_интервал_всегда_в_допустимых_границах(seconds: float) -> None:
    ctl = make_control()
    value = await ctl.controller.set_interval(seconds)
    assert MIN_POLL_INTERVAL_SEC <= value <= MAX_POLL_INTERVAL_SEC
    assert ctl.rig.runtime.base_interval_sec == value


# --- пауза и продолжение ---


async def test_пауза_на_минуты_ставит_срок_сохраняет_и_пишет_в_журнал() -> None:
    ctl = make_control()
    until = await ctl.controller.pause(30)
    assert until == T0 + timedelta(minutes=30)
    assert ctl.rig.runtime.paused_until == until
    assert ctl.kv.data[K_PAUSED_UNTIL] == until.isoformat()
    assert "command.pause" in ctl.rig.journal.codes()
    view = await ctl.controller.status()
    assert view.paused_until == until


@pytest.mark.parametrize("minutes", [None, 0])
async def test_пауза_без_срока_это_бессрочная_пауза(minutes: int | None) -> None:
    # 0 минут в меню Telegram означает «без срока» (`int(item) or None`)
    ctl = make_control()
    until = await ctl.controller.pause(minutes)
    assert until == T0 + FOREVER


async def test_продолжение_снимает_паузу_и_сохраняет() -> None:
    ctl = make_control()
    await ctl.controller.pause(30)
    await ctl.controller.resume()
    assert ctl.rig.runtime.paused_until is None
    assert ctl.kv.data[K_PAUSED_UNTIL] == ""
    assert "command.resume" in ctl.rig.journal.codes()
    assert (await ctl.controller.status()).paused_until is None


async def test_истёкшая_пауза_в_статусе_не_показывается() -> None:
    ctl = make_control()
    await ctl.controller.pause(1)
    await ctl.rig.clock.advance(61)
    assert (await ctl.controller.status()).paused_until is None


# --- месяцы и каналы ---


@pytest.mark.parametrize(
    ("asked", "expected"), [(0, 1), (-5, 1), (1, 1), (6, 6), (12, 12), (99, 12)]
)
async def test_месяцы_вперёд_ограничены_от_1_до_12(asked: int, expected: int) -> None:
    ctl = make_control()
    assert await ctl.controller.set_months(asked) == expected
    assert ctl.kv.data[K_MONTHS] == str(expected)
    assert "command.months" in ctl.rig.journal.codes()


async def test_переключение_канала_сохраняется_и_влияет_на_рассылку() -> None:
    ctl = make_control()
    assert await ctl.controller.toggle_channel(Channel.NTFY) is False
    assert ctl.kv.data[K_CHANNELS_OFF] == "ntfy"
    assert (await ctl.controller.status()).channels_off == frozenset({Channel.NTFY})
    await ctl.rig.alerts.on_appeared(appeared(23))
    assert ctl.rig.attempts(Channel.NTFY) == []
    assert ctl.rig.attempts(Channel.TELEGRAM) == [1]
    assert await ctl.controller.toggle_channel(Channel.NTFY) is True
    assert ctl.kv.data[K_CHANNELS_OFF] == ""
    assert (await ctl.controller.status()).channels_off == frozenset()
    assert ctl.rig.journal.codes().count("command.channel") == 2
    await ctl.rig.alerts.close()


async def test_несколько_выключенных_каналов_сохраняются_в_стабильном_порядке() -> None:
    ctl = make_control()
    await ctl.controller.toggle_channel(Channel.TELEGRAM)
    await ctl.controller.toggle_channel(Channel.CALL)
    assert ctl.kv.data[K_CHANNELS_OFF] == "call,telegram"


# --- тест каналов, «Вижу», «Готово» ---


async def test_тест_одного_канала_и_всех_пишет_команду_в_журнал() -> None:
    ctl = make_control()
    [one] = await ctl.controller.test(Channel.CALL)
    assert one.channel is Channel.CALL
    assert len(await ctl.controller.test()) == 3
    messages = [m for code, m, *_ in ctl.rig.journal.events if code == "command.test"]
    assert messages == ["тест каналов: call", "тест каналов: все"]


async def test_ack_делегируется_рассылке() -> None:
    ctl = make_control()
    assert await ctl.controller.ack() is False
    await ctl.rig.book.on_appeared(appeared(23))
    assert await ctl.controller.ack() is True
    assert ctl.rig.repo.incidents[1].acked_at is not None


async def test_готово_пишет_в_журнал_и_будит_цикл_опроса() -> None:
    ctl = make_control()
    task = asyncio.create_task(ctl.loop.run())
    await settle()
    assert len(ctl.source.calls) == 1  # первая проверка; следующая — через минуту
    ctl.controller.manual_done()
    await settle()
    assert len(ctl.source.calls) == 2  # владелец прошёл шаг 1: проверка сразу
    assert "command.manual_done" in ctl.rig.journal.codes()
    ctl.loop.stop()
    await ctl.rig.clock.advance(1)
    await task


async def test_смена_интервала_будит_цикл_опроса() -> None:
    ctl = make_control()
    task = asyncio.create_task(ctl.loop.run())
    await settle()
    await ctl.controller.set_interval(30.0)
    await settle()
    assert len(ctl.source.calls) == 2
    ctl.loop.stop()
    await ctl.rig.clock.advance(1)
    await task


# --- повторный вход ---


async def test_relogin_успешный_возвращает_пустую_строку_и_идёт_принудительно() -> None:
    ctl = make_control()
    await ctl.rig.session.relogin("первый вход")
    assert ctl.rig.session.has_session
    assert await ctl.controller.relogin() == ""
    assert ctl.rig.session.logins == 2  # сессия была, но вход по команде выполнен всё равно
    assert "command.relogin" in ctl.rig.journal.codes()


@pytest.mark.parametrize(
    "error", [LoginFailed("неверная страница входа"), ReloginBudgetExceeded("6 входов за час")]
)
async def test_relogin_ошибка_бота_возвращается_строкой(error: Exception) -> None:
    ctl = make_control()
    ctl.rig.session._auth = FakeAuthenticator(failures=[error])
    assert await ctl.controller.relogin() == str(error)


async def test_relogin_отложен_после_тревоги_и_сообщает_об_этом_строкой() -> None:
    ctl = make_control()
    ctl.rig.session.quiet_after_alert()
    text = await ctl.controller.relogin()
    assert "отложен" in text and "после тревоги" in text
    assert ctl.rig.session.logins == 0


async def test_relogin_неожиданное_исключение_не_маскируется() -> None:
    ctl = make_control()
    ctl.rig.session._auth = FakeAuthenticator(failures=[RuntimeError("баг")])
    with pytest.raises(RuntimeError):
        await ctl.controller.relogin()


# --- очистка, инциденты, ошибки ---


async def test_cleanup_вызывает_очистку_и_возвращает_удалённое() -> None:
    ctl = make_control(cleanup_result={"quarantine": 3})
    assert await ctl.controller.cleanup() == {"quarantine": 3}
    assert ctl.cleanups == [1]
    assert "command.cleanup" in ctl.rig.journal.codes()


async def test_cleanup_без_обработчика_возвращает_пустой_словарь() -> None:
    ctl = make_control(with_hooks=False)
    assert await ctl.controller.cleanup() == {}


async def test_last_возвращает_последние_инциденты_с_доставками_новые_первыми() -> None:
    ctl = make_control()
    for day in (1, 2, 3):
        await ctl.rig.book.on_appeared(appeared(day))
        await ctl.rig.book.record(DeliveryResult(Channel.TELEGRAM, True, 5.0, 1))
        await ctl.rig.book.on_gone(gone())
    views = await ctl.controller.last(2)
    assert [v.incident.id for v in views] == [3, 2]
    assert all(len(v.deliveries) == 1 for v in views)
    assert [v.incident.id for v in await ctl.controller.last()] == [3, 2, 1]
    assert await make_control().controller.last() == []


async def test_recent_errors_только_предупреждения_и_выше_с_лимитом() -> None:
    ctl = make_control()
    for i, level in enumerate(["INFO", "WARNING", "ERROR", "DEBUG", "WARNING"]):
        await ctl.event_log.write(AppEvent(T0 + timedelta(seconds=i), level, f"c{i}", f"m{i}"))
    errors = await ctl.controller.recent_errors(2)
    assert [e.code for e in errors] == ["c4", "c2"]
    assert {e.level for e in await ctl.controller.recent_errors(10)} == {"WARNING", "ERROR"}


# --- статус ---


async def test_статус_содержит_всё_нужное_для_telegram() -> None:
    ctl = make_control(folder_bytes=7 * 2**20, journal_bytes=2**20)
    ctl.liveness_text = "проверка идёт 200 с"
    await ctl.controller.toggle_channel(Channel.CALL)
    await ctl.rig.session.relogin("тест")
    await ctl.rig.book.on_appeared(appeared(23, 24))
    await ctl.rig.alerts.on_appeared(appeared(23, 24))
    view = await ctl.controller.status()
    assert view.now == T0
    assert view.status is ctl.loop.status
    assert view.status.state is PollState.RUNNING
    assert view.source is SourceKind.DOM
    assert view.base_interval_sec == 20.0
    assert view.interval_sec == 60.0  # лестница D4: первые 6 часов — 60 с, DOM — не реже 60 с
    assert view.months_ahead == 6
    assert view.paused_until is None
    assert view.incident is not None and view.incident.slots == slots(23, 24)
    assert view.repeating is True
    assert view.logins_last_hour == 1
    assert view.quiet_remaining_sec == 600.0
    assert view.channels_off == frozenset({Channel.CALL})
    assert view.folder_bytes == 7 * 2**20
    assert view.journal_bytes == 2**20
    assert view.hung_reason == "проверка идёт 200 с"
    await ctl.rig.alerts.close()


async def test_статус_без_необязательных_источников_данных() -> None:
    ctl = make_control(with_hooks=False)
    view = await ctl.controller.status()
    assert view.folder_bytes is None and view.journal_bytes is None
    assert view.hung_reason == ""
    assert view.incident is None and not view.repeating
    assert view.quiet_remaining_sec == 0.0


async def test_статус_отражает_смену_интервала_и_месяцев() -> None:
    ctl = make_control()
    await ctl.controller.set_interval(300.0)
    await ctl.controller.set_months(3)
    await ctl.rig.clock.advance(19 * 3600)  # лестница закончилась
    status = await ctl.controller.status()
    assert status.base_interval_sec == 300.0 and status.interval_sec == 300.0
    assert status.months_ahead == 3
