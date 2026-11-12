"""Сценарий 6: сайт требует шаг 1 — бот зовёт владельца кнопкой и не решает вопрос (D3)."""

from __future__ import annotations

import pytest

from rcbot.domain.models import AlertAction, Channel, PollState
from rcbot.infrastructure.notify.telegram import CALLBACK_MANUAL_DONE, keyboard
from tests.e2e import site as fake
from tests.e2e.helpers import D23
from tests.e2e.site import FakeSite


def step1(site: FakeSite, times: int = 20) -> None:
    site.push(fake.html(fake.STEP1_HTML), times=times)


def manual_messages(stand) -> list:
    return [s for s in stand.sent(Channel.TELEGRAM) if s.alert.action is AlertAction.MANUAL_DONE]


async def test_шаг_1_одно_сообщение_с_кнопкой_готово_и_ни_одного_запроса_кроме_get(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    step1(site)
    await stand.next_check()

    assert stand.loop.status.state is PollState.NEEDS_MANUAL
    (message,) = manual_messages(stand)
    assert "шаг 1" in message.alert.text
    button = keyboard(message.alert.action)
    assert button is not None
    assert button.inline_keyboard[0][0].callback_data == CALLBACK_MANUAL_DONE
    # только Telegram: ни звонка, ни push
    assert not stand.sent(Channel.NTFY) and not stand.sent(Channel.CALL)
    assert "manual.required" in stand.journal.codes()
    # бот ничего не отвечает на вопрос: на сайт уходят одни GET-запросы
    assert {r.method for r in site.server.requests} == {"GET"}


async def test_пока_шаг_1_не_пройден_перепроверка_раз_в_15_минут_без_спама(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    step1(site)
    await stand.next_check()
    requests = len(site.calls)

    await stand.advance(899)
    assert len(site.calls) == requests
    await stand.advance(3 * 900)  # три перепроверки, шаг 1 всё ещё требуется
    assert len(site.calls) == requests + 3
    assert len(manual_messages(stand)) == 1, "владельца не спамят на каждую перепроверку"
    assert len(stand.texts(Channel.TELEGRAM)) == 1


async def test_готово_будит_цикл_проверка_идёт_немедленно(make_stand, site: FakeSite) -> None:
    stand = await make_stand(site=site)
    step1(site)
    await stand.next_check()
    site.script.clear()  # владелец прошёл шаг 1
    site.slots = list(D23)
    requests, waited = len(site.calls), stand.elapsed

    stand.controller.manual_done()
    await stand.quiesce()

    assert len(site.calls) == requests + 1, "проверка сразу, а не через 15 минут"
    assert stand.elapsed == pytest.approx(waited), "часы стенда не двигались"
    assert stand.loop.status.state is PollState.RUNNING
    assert "command.manual_done" in stand.journal.codes()
    # места, найденные сразу после шага 1, приходят всем каналам
    assert all(stand.sent(c) for c in Channel)
    assert any("снова работает" in m for m in stand.texts(Channel.TELEGRAM))


async def test_если_владелец_не_нажал_готово_бот_сам_заметит_через_15_минут(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    step1(site, times=1)
    await stand.next_check()
    t = stand.elapsed

    await stand.advance(900)
    assert stand.elapsed - t == pytest.approx(900)
    assert stand.loop.status.state is PollState.RUNNING
    assert site.calls[-1].mono - stand.t0 == pytest.approx(t + 900)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG-T9: poll_loop.py:_wait — после wake() следующая проверка идёт сразу, минимум 10 с "
        "между запросами (SPEC 0.3, правило 3) не соблюдается: двойное нажатие «Готово»/"
        "/interval/resume даёт два запроса подряд"
    ),
)
async def test_двойное_нажатие_готово_не_нарушает_минимум_10_секунд_между_запросами(
    make_stand, site: FakeSite
) -> None:
    stand = await make_stand(site=site)
    stand.controller.manual_done()
    await stand.quiesce()
    await stand.advance(1)
    stand.controller.manual_done()
    await stand.quiesce()

    last = [c.mono for c in site.calls[-2:]]
    assert last[1] - last[0] >= 10
