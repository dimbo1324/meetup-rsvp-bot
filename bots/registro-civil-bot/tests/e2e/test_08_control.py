"""Сценарий 8: управление из Telegram во время работы (Controller) и его влияние на цикл."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from rcbot.application.control import IntervalTooSmall
from rcbot.application.runtime import K_CHANNELS_OFF, K_INTERVAL, K_PAUSED_UNTIL
from rcbot.bot import texts
from rcbot.domain.errors import LoginFailed
from rcbot.domain.models import Channel, PollState, SourceKind
from tests.e2e.helpers import D23, gaps
from tests.e2e.site import FakeSite
from tests.e2e.stand import fake_source
from tests.fakes import slots

# --- пауза ---


async def test_пауза_на_30_минут_останавливает_запросы_и_кончается_сама(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    until = await stand.controller.pause(30)
    await stand.quiesce()
    calls = len(site.calls)

    assert until == stand.clock.now() + timedelta(minutes=30)
    assert stand.loop.status.state is PollState.PAUSED
    assert await stand.storage.kv.get(K_PAUSED_UNTIL) == until.isoformat()
    await stand.advance(30 * 60 - 1)
    assert len(site.calls) == calls and stand.loop.status.state is PollState.PAUSED
    await stand.advance(1)
    assert len(site.calls) == calls + 1, "первая проверка — в момент окончания паузы"
    assert stand.loop.status.state is PollState.RUNNING
    assert "command.pause" in stand.journal.codes()


async def test_бессрочная_пауза_и_resume_проверка_сразу(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    await stand.controller.pause(None)
    await stand.advance(24 * 3600)
    calls = len(site.calls)
    assert stand.loop.status.state is PollState.PAUSED
    assert stand.loop.status.state is PollState.PAUSED and stand.session.logins == 1

    await stand.controller.resume()
    await stand.quiesce()
    assert len(site.calls) == calls + 1
    assert stand.loop.status.state is PollState.RUNNING
    assert await stand.storage.kv.get(K_PAUSED_UNTIL) == ""
    assert "command.resume" in stand.journal.codes()


async def test_на_паузе_тревога_не_приходит_даже_если_места_появились(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    await stand.controller.pause(10)
    site.slots = list(D23)
    await stand.advance(9 * 60)
    assert all(not stand.sent(c) for c in Channel)
    await stand.advance(60)  # пауза кончилась — проверка нашла места
    assert all(stand.sent(c) for c in Channel)


# --- интервал ---


async def test_интервал_меньше_10_секунд_отклонён_граница_10_принята(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site, ladder_age_hours=20)
    for bad in (9.999, 0, -5):
        with pytest.raises(IntervalTooSmall):
            await stand.controller.set_interval(bad)
    assert stand.runtime.base_interval_sec == 20
    assert await stand.storage.kv.get(K_INTERVAL) in (None, "20"), "в БД не попало 9.999"

    assert await stand.controller.set_interval(10) == 10
    await stand.quiesce()
    await stand.checks(3)
    assert gaps(site, 4) == [10, 10, 10]  # SPEC 0.3: ниже 10 с нельзя, но 10 — можно


async def test_интервал_меняет_расписание_и_сверху_ограничен(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site, ladder_age_hours=20)
    assert await stand.controller.set_interval(45) == 45
    await stand.quiesce()
    await stand.checks(3)
    # первая проверка по команде — через минимум (10 с) после предыдущей, дальше — по 45 с
    assert gaps(site, 4) == [10, 45, 45]
    assert await stand.controller.set_interval(100_000) == 3600
    assert "command.interval" in stand.journal.codes()


async def test_интервал_nan_отклоняется(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site, ladder_age_hours=20)
    with pytest.raises((IntervalTooSmall, ValueError)):
        await stand.controller.set_interval(float("nan"))


# --- каналы и тест ---


async def test_выключенный_канал_не_получает_тревог_и_включается_обратно(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    assert await stand.controller.toggle_channel(Channel.NTFY) is False
    assert await stand.storage.kv.get(K_CHANNELS_OFF) == "ntfy"
    site.slots = list(D23)
    await stand.next_check()
    assert not stand.sent(Channel.NTFY)
    assert stand.sent(Channel.TELEGRAM) and stand.sent(Channel.CALL)
    # повторы выключенного канала тоже молчат
    await stand.advance(600)
    assert not stand.sent(Channel.NTFY)

    assert await stand.controller.toggle_channel(Channel.NTFY) is True
    assert await stand.storage.kv.get(K_CHANNELS_OFF) == ""
    assert stand.runtime.channel_enabled(Channel.NTFY)


async def test_тест_каналов_идёт_во_все_и_не_создаёт_инцидентов_и_доставок(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    await stand.controller.toggle_channel(Channel.CALL)  # даже выключенный канал проверяется
    results = await stand.controller.test()
    assert sorted(r.channel for r in results) == sorted(Channel) and all(r.ok for r in results)
    assert "Это тест" in stand.texts(Channel.TELEGRAM)[-1]
    assert await stand.incidents() == []
    one = await stand.controller.test(Channel.NTFY)
    assert [r.channel for r in one] == [Channel.NTFY]
    assert "command.test" in stand.journal.codes()
    assert not stand.alerts.repeating(), "проверка канала повторов не запускает"


# --- вход, подтверждение, статус, список, очистка ---


async def test_команда_relogin_входит_сразу_и_будит_проверку(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    calls, last = len(site.calls), site.calls[-1].mono
    assert await stand.controller.relogin() == ""
    await stand.quiesce()
    assert stand.session.logins == 2 and len(site.calls) == calls, "минимум 10 с ещё не прошёл"
    await stand.advance(10)
    assert len(site.calls) == calls + 1 and site.calls[-1].cookie.endswith("SESSION-2-1")
    assert site.calls[-1].mono - last == 10
    assert "command.relogin" in stand.journal.codes()


async def test_команда_relogin_возвращает_причину_отказа_и_подчиняется_лимиту(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    stand.auth._failures.append(LoginFailed("сайт отказал во входе"))  # type: ignore[attr-defined]
    assert "сайт отказал" in await stand.controller.relogin()

    for _ in range(4):  # попытки № 3–6 за час (№ 1 — старт, № 2 — отказ)
        assert await stand.controller.relogin() == ""
    assert "лимит 6" in await stand.controller.relogin()


async def test_ack_без_тревоги_ничего_не_подтверждает(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    assert await stand.controller.ack() is False


async def test_статус_и_последние_инциденты_собираются_из_настоящих_данных(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    site.slots = list(D23)
    await stand.next_check()
    await stand.controller.ack()

    view = await stand.controller.status()
    assert view.status.state is PollState.RUNNING and view.source is SourceKind.API
    assert view.incident is not None and view.incident.acked and not view.repeating
    text = texts.status_text(view, stand.settings.tz)
    assert "Состояние: работает" in text and "Источник: API" in text
    assert "Открыт инцидент №1: 3 мест, тревога подтверждена" in text

    (item,) = await stand.controller.last()
    assert len(item.deliveries) == 3
    assert "доставок: 3 из 3, подтверждён" in texts.last_text(
        await stand.controller.last(), stand.settings.tz
    )


async def test_очистка_по_команде_удаляет_старые_файлы_и_пишет_в_журнал(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    old = stand.paths.quarantine / "old.json"
    fresh = stand.paths.quarantine / "fresh.json"
    old.write_text("{}", "utf-8")
    fresh.write_text("{}", "utf-8")
    stale = (stand.clock.now() - timedelta(days=10)).timestamp()
    os.utime(old, (stale, stale))
    os.utime(fresh, (stand.clock.now().timestamp(),) * 2)

    removed = await stand.controller.cleanup()
    assert removed.get("quarantine") == 1
    assert not old.exists() and fresh.exists()
    assert "command.cleanup" in stand.journal.codes()


async def test_число_месяцев_доходит_до_источника(make_stand, stand_clock) -> None:
    source = fake_source(stand_clock, slots(), kind=SourceKind.API)
    stand = await make_stand(primary=source, auth=_NoLogin())
    assert await stand.controller.set_months(99) == 12
    assert await stand.controller.set_months(9) == 9
    await stand.next_check()
    assert source.calls[-1].months_ahead == 9


class _NoLogin:
    async def login(self) -> list:
        return []
