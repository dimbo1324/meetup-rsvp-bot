"""Сценарий 3: истечение сессии (302 на /autenticacion), вход, лимит 6 в час, пауза D5."""

from __future__ import annotations

import pytest

from rcbot.domain.models import Channel, PollState
from tests.e2e.helpers import D23, MIN_GAP, collect_events
from tests.e2e.site import FakeSite


async def test_302_на_вход_ведёт_к_повторному_входу_и_cookies_уходят_на_сервер(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    events = collect_events(stand)
    assert site.calls[-1].cookie == "simple_bpm_session=SESSION-1-0"

    site.expire_sessions()
    await stand.next_check()
    # вход выполнен сразу, но повторный запрос не раньше минимума между проверками
    assert stand.session.logins == 2 and site.calls[-1].cookie.endswith("SESSION-1-0")
    t_302 = site.calls[-1].mono
    await stand.advance(MIN_GAP - 0.5)
    assert site.calls[-1].mono == t_302
    await stand.advance(0.5)

    # старая cookie → 302; вход; повторный запрос уже с новой cookie и она принята
    assert site.calls[-1].mono - t_302 == MIN_GAP
    assert [c.cookie for c in site.calls[-2:]] == [
        "simple_bpm_session=SESSION-1-0",
        "simple_bpm_session=SESSION-2-1",
    ]
    assert stand.session.logins == 2 and stand.loop.status.state is PollState.RUNNING
    assert [(e.old, e.new) for e in events if hasattr(e, "old")] == [
        (PollState.RUNNING, PollState.REAUTH),
        (PollState.REAUTH, PollState.RUNNING),
    ]
    checks = stand.journal.read_checks()
    assert checks[-2]["error_kind"] == "SessionExpired" and checks[-1]["ok"] is True
    # вход не будит владельца: служебных сообщений нет
    assert stand.texts(Channel.TELEGRAM) == []


async def test_сессия_живёт_между_проверками_без_лишних_входов(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    await stand.checks(5)
    assert stand.session.logins == 1
    assert {c.cookie for c in site.calls[1:]} == {"simple_bpm_session=SESSION-1-0"}


async def exhaust_login_budget(stand, site: FakeSite) -> None:
    """Шесть входов за час уже выполнены (первый — при старте), сессия истекает в седьмой раз."""
    for _ in range(5):
        site.expire_sessions()
        await stand.next_check()
        await stand.advance(MIN_GAP)  # повторная проверка после входа
    assert stand.session.logins == 6
    assert stand.loop.status.state is PollState.RUNNING
    site.expire_sessions()
    await stand.next_check()


async def test_лимит_шесть_входов_в_час_седьмой_уходит_в_backoff_и_сообщает(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)  # вход № 1
    await exhaust_login_budget(stand, site)

    assert stand.session.logins == 6, "седьмой вход не выполнен"
    assert stand.loop.status.state is PollState.BACKOFF
    assert len(stand.texts(Channel.TELEGRAM)) == 1
    assert "6 входов за час" in stand.texts(Channel.TELEGRAM)[0]
    requests = len(site.calls)
    await stand.advance(899)
    assert len(site.calls) == requests, "пауза 15 минут: запросов к сайту нет"


async def test_после_часа_лимит_освобождается_и_вход_выполняется(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    await exhaust_login_budget(stand, site)
    await stand.advance(3600)

    assert stand.session.logins >= 7
    assert stand.loop.status.state is PollState.RUNNING


async def test_после_лимита_входов_владелец_узнаёт_что_опрос_снова_работает(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    await exhaust_login_budget(stand, site)
    await stand.advance(3600)

    assert any("снова работает" in m for m in stand.texts(Channel.TELEGRAM))


async def test_исчерпанный_лимит_входов_даёт_одно_сообщение_а_не_по_одному_на_попытку(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    await exhaust_login_budget(stand, site)
    await stand.advance(3600)  # четыре повторные попытки по 15 минут, лимит ещё занят

    slowdowns = [m for m in stand.texts(Channel.TELEGRAM) if "замедлился" in m]
    assert len(slowdowns) == 1, slowdowns


async def test_после_тревоги_десять_минут_без_входа(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    site.slots = list(D23)
    await stand.next_check()  # t = 60: тревога
    t_alert = stand.elapsed
    assert stand.session.quiet_remaining() == pytest.approx(600)

    site.expire_sessions()
    await stand.next_check()  # t = 120: 302, вход отложен
    assert stand.session.logins == 1
    assert stand.loop.status.state is PollState.REAUTH
    requests = len(site.calls)

    await stand.advance(t_alert + 600 - stand.elapsed - 1)  # t = тревога + 599 с
    assert stand.session.logins == 1 and len(site.calls) == requests
    await stand.advance(1)  # ровно тревога + 600 с: вход, но запрос — не раньше минимума
    assert stand.session.logins == 2 and len(site.calls) == requests + 1
    await stand.advance(MIN_GAP)
    assert site.calls[-1].cookie == "simple_bpm_session=SESSION-2-1"
    assert stand.loop.status.state is PollState.RUNNING
    # отложенный вход не считался неудачей: «замедлился» не прозвучало
    assert not any("замедлился" in m for m in stand.texts(Channel.TELEGRAM))


async def test_ручной_вход_во_время_тишины_отклонён_с_объяснением(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    site.slots = list(D23)
    await stand.next_check()

    error = await stand.controller.relogin()
    assert "отложен" in error and stand.session.logins == 1
