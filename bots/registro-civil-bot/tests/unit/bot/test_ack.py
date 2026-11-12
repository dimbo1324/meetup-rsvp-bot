"""Подтверждение тревоги: кнопки «Вижу» и «Готово», слово «ок» (SPEC 2.4, D3)."""

from __future__ import annotations

import pytest

from rcbot.bot import texts
from rcbot.domain.events import SlotsAppeared
from rcbot.infrastructure.notify.telegram import CALLBACK_ACK, CALLBACK_MANUAL_DONE
from tests.fakes import T0, slots, snapshot
from tests.unit.bot.harness import Setup, make_setup, stranger_of


async def alarm(setup: Setup) -> None:
    """Настоящая тревога: открытый инцидент и идущие повторы во всех каналах."""
    event = SlotsAppeared(T0, snapshot(slots(23, 24)))
    await setup.rig.book.on_appeared(event)
    await setup.rig.alerts.on_appeared(event)
    await setup.rig.clock.advance(0)
    assert setup.rig.alerts.repeating()


def first_waves(setup: Setup) -> int:
    return sum(len(n.sent) for n in setup.rig.notifiers.values())


# --- кнопка «Вижу» ---


async def test_вижу_при_тревоге_останавливает_повторы_и_подтверждает_инцидент() -> None:
    setup = await make_setup()
    await alarm(setup)
    sent_before = first_waves(setup)
    await setup.owner.press(CALLBACK_ACK)
    (answer,) = setup.session.answers
    assert answer.text == "Принято" and answer.callback_query_id == "cb1"
    assert setup.session.sent_texts == [texts.ACKED]
    assert setup.rig.alerts.repeating() is False
    assert (await setup.rig.book.current()).acked  # type: ignore[union-attr]
    await setup.rig.clock.advance(3600)
    assert first_waves(setup) == sent_before  # повторов больше нет ни в одном канале


async def test_вижу_без_тревоги_отвечает_тревоги_нет_и_молчит_в_чате() -> None:
    setup = await make_setup()
    await setup.owner.press(CALLBACK_ACK)
    assert [a.text for a in setup.session.answers] == ["Тревоги нет"]
    assert setup.session.sent == []


async def test_повторное_вижу_после_подтверждения_безопасно() -> None:
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.press(CALLBACK_ACK)
    await setup.owner.press(CALLBACK_ACK)
    assert len(setup.session.answers) == 2  # на каждое нажатие есть ответ, иначе кнопка «крутится»


async def test_вижу_без_сообщения_отвечает_но_не_пишет_в_чат() -> None:
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.press(CALLBACK_ACK, with_message=False)
    assert [a.text for a in setup.session.answers] == ["Принято"]
    assert setup.session.sent == []
    assert setup.rig.alerts.repeating() is False


async def test_ответ_на_нажатие_не_длиннее_лимита_telegram() -> None:
    setup = await make_setup()
    await setup.owner.press(CALLBACK_ACK)
    assert all(len(a.text or "") <= 200 for a in setup.session.answers)


# --- кнопка «Готово» ---


async def test_готово_будит_цикл_и_благодарит() -> None:
    setup = await make_setup()
    setup.rig.loop._wake.clear()
    await setup.owner.press(CALLBACK_MANUAL_DONE)
    assert [a.text for a in setup.session.answers] == [texts.MANUAL_THANKS]
    assert setup.rig.loop._wake.is_set()
    assert "command.manual_done" in setup.rig.journal.codes()


async def test_готово_не_подтверждает_тревогу_и_не_останавливает_повторы() -> None:
    """«Готово» — про шаг 1, «Вижу» — про места: смешивать их нельзя."""
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.press(CALLBACK_MANUAL_DONE)
    assert setup.rig.alerts.repeating() is True
    assert not (await setup.rig.book.current()).acked  # type: ignore[union-attr]


async def test_неизвестная_кнопка_ничего_не_делает() -> None:
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.press("unknown_action")
    assert setup.rig.alerts.repeating() is True
    assert setup.session.sent == []


# --- слово «ок» ---


@pytest.mark.parametrize(
    "word",
    ["ок", "ОК", "Ок", "оК", "окей", "Окей", "ok", "OK", "Ok", "okay", "OKAY", "вижу", "Вижу"],
)
async def test_слово_подтверждает_тревогу(word: str) -> None:
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.say(word)
    assert setup.session.sent_texts == [texts.ACKED]
    assert setup.rig.alerts.repeating() is False


@pytest.mark.parametrize(
    "word", ["ок.", "ок!", "ок!!!", "ок...", "  ок  ", "\tок\n", "OK!", "вижу."]
)
async def test_слово_с_пунктуацией_и_пробелами(word: str) -> None:
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.say(word)
    assert setup.session.sent_texts == [texts.ACKED]


async def test_слово_без_тревоги_отвечает_что_подтверждать_нечего() -> None:
    setup = await make_setup()
    await setup.owner.say("ок")
    assert setup.session.sent_texts == [texts.NOT_ACKED]
    assert setup.rig.incidents.incidents == {}


@pytest.mark.parametrize(
    "text",
    [
        "окей, а что дальше?",
        "ок ок",
        "ок?",
        "ок, спасибо",
        "нет",
        "привет",
        "okk",
        "оке",
        "хорошо",
        "10",
        "/ок",
        "не ок",
        "окей\nа дальше",
        "",
        " ",
    ],
)
async def test_другой_текст_тревогу_не_подтверждает_и_без_ответа(text: str) -> None:
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.say(text)
    assert setup.session.sent == []
    assert setup.rig.alerts.repeating() is True
    assert not (await setup.rig.book.current()).acked  # type: ignore[union-attr]


async def test_сообщение_без_текста_игнорируется() -> None:
    """Фото, стикер и пр.: `text` пуст — регэксп не должен падать на None."""
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.say(None)
    assert setup.session.sent == [] and setup.rig.alerts.repeating() is True


async def test_инцидент_открыт_пока_места_есть_даже_после_подтверждения() -> None:
    setup = await make_setup()
    await alarm(setup)
    await setup.owner.say("ок")
    incident = await setup.rig.book.current()
    assert incident is not None and incident.acked and incident.status.value == "open"


# --- чужой не подтверждает ---


async def test_чужое_слово_ок_не_останавливает_повторы() -> None:
    setup = await make_setup()
    await alarm(setup)
    await stranger_of(setup).say("ок")
    await stranger_of(setup).press(CALLBACK_ACK)
    assert setup.rig.alerts.repeating() is True
    assert setup.session.calls == []
