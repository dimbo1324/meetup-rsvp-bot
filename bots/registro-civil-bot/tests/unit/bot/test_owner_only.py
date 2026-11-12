"""Чужой не управляет ботом ни одной командой и ни одной кнопкой (инвариант 10, SPEC 2.4)."""

from __future__ import annotations

import logging

import pytest
from aiogram.types import Message

from rcbot.domain.events import SlotsAppeared
from rcbot.infrastructure.notify.telegram import CALLBACK_ACK, CALLBACK_MANUAL_DONE
from tests.fakes import T0, slots, snapshot
from tests.unit.bot.harness import (
    GROUP,
    OWNER,
    STRANGER,
    Chatter,
    Setup,
    make_setup,
    snapshot_of_state,
)

COMMANDS = [
    "/start",
    "/help",
    "/status",
    "/pause",
    "/pause 30",
    "/resume",
    "/interval 15",
    "/interval 999",
    "/test",
    "/last",
    "/relogin",
    "/cleanup",
    "/menu",
    "ок",
    "вижу",
    "привет",
]

# (user_id, chat_id, тип чата): кто и откуда пишет
OUTSIDERS = {
    "чужой в личке": (STRANGER, STRANGER, "private"),
    "чужой в чате владельца": (STRANGER, OWNER, "private"),
    "владелец в группе": (OWNER, GROUP, "supergroup"),
    "владелец в чужом чате": (OWNER, STRANGER, "private"),
}


async def armed(setup: Setup) -> None:
    """Идёт настоящая тревога: повторы запущены, и их можно (ошибочно) остановить."""
    event = SlotsAppeared(T0, snapshot(slots(23)))
    await setup.rig.book.on_appeared(event)
    await setup.rig.alerts.on_appeared(event)
    await setup.rig.clock.advance(0)


def outsider(setup: Setup, who: str) -> Chatter:
    user, chat, chat_type = OUTSIDERS[who]
    return Chatter(setup.dp, setup.bot, user_id=user, chat_id=chat, chat_type=chat_type)


@pytest.mark.parametrize("who", list(OUTSIDERS))
@pytest.mark.parametrize("text", COMMANDS)
async def test_команда_чужого_молча_отбрасывается_и_ничего_не_меняет(who: str, text: str) -> None:
    setup = await make_setup()
    await armed(setup)
    before = snapshot_of_state(setup.rig)
    calls_before = len(setup.session.calls)
    await outsider(setup, who).say(text)
    assert len(setup.session.calls) == calls_before, "бот не должен отвечать чужому ничего"
    assert snapshot_of_state(setup.rig) == before


@pytest.mark.parametrize("who", list(OUTSIDERS))
async def test_кнопки_ack_и_готово_у_чужого_не_работают(who: str) -> None:
    setup = await make_setup()
    await armed(setup)
    before = snapshot_of_state(setup.rig)
    for data in (CALLBACK_ACK, CALLBACK_MANUAL_DONE):
        await outsider(setup, who).press(data)
    assert setup.session.calls == []
    assert snapshot_of_state(setup.rig) == before
    assert setup.rig.alerts.repeating() is True


@pytest.mark.parametrize("who", list(OUTSIDERS))
async def test_кнопки_меню_у_чужого_не_работают(who: str) -> None:
    """Чужой нажимает любые кнопки настоящего меню владельца: ни одна не должна сработать."""
    setup = await make_setup()
    await armed(setup)
    await setup.owner.say("/menu")
    markup = setup.session.sent[0].reply_markup
    intruder = outsider(setup, who)
    # Кнопки настоящего меню, но сообщение лежит в том чате, откуда пишет нарушитель
    menu = Message(
        message_id=500,
        date=1_700_000_000,  # type: ignore[arg-type]
        chat=intruder.chat,
        text="Меню rcbot",
        reply_markup=markup,  # type: ignore[arg-type]
    )
    datas = [
        b.callback_data
        for row in markup.inline_keyboard  # type: ignore[union-attr]
        for b in row
        if b.callback_data
    ]
    assert datas, "в меню должны быть кнопки"
    calls_before = len(setup.session.calls)
    before = snapshot_of_state(setup.rig)
    for data in datas:
        await intruder.press(data, on=menu)
    assert len(setup.session.calls) == calls_before
    assert snapshot_of_state(setup.rig) == before


async def test_владелец_после_чужих_попыток_работает_как_раньше() -> None:
    setup = await make_setup()
    await outsider(setup, "чужой в личке").say("/pause")
    await setup.owner.say("/status")
    assert setup.session.sent_texts[0].startswith("Состояние: работает")
    assert setup.rig.runtime.paused_until is None


async def test_чужие_обновления_попадают_в_журнал_без_номера(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    setup = await make_setup()
    await outsider(setup, "чужой в личке").say("/status")
    await outsider(setup, "чужой в личке").press(CALLBACK_ACK)
    ours = [r.getMessage() for r in caplog.records if r.name == "rcbot.bot.middlewares"]
    assert len(ours) == 2 and all(str(STRANGER) not in line for line in ours)
