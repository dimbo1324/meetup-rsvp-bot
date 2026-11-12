"""Команды SPEC 2.5 через настоящий Dispatcher: разбор аргументов, ответы, побочные эффекты."""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from rcbot.bot import texts
from rcbot.config.settings import MAX_POLL_INTERVAL_SEC, MIN_POLL_INTERVAL_SEC
from rcbot.domain.errors import LoginFailed, ReloginBudgetExceeded, ReloginDeferred
from rcbot.domain.events import SlotsAppeared
from rcbot.domain.models import Channel
from tests.fakes import T0, slots, snapshot
from tests.unit.bot.harness import Setup, make_setup


async def appear(setup: Setup) -> None:
    """Открытый инцидент и идущие повторы, как после настоящей тревоги."""
    event = SlotsAppeared(T0, snapshot(slots(23, 24)))
    await setup.rig.book.on_appeared(event)
    await setup.rig.alerts.on_appeared(event)
    await setup.rig.clock.advance(0)


# --- /start, /help ---


@pytest.mark.parametrize("command", ["/start", "/help"])
async def test_старт_и_справка_отвечают_справкой(command: str) -> None:
    setup = await make_setup()
    await setup.owner.say(command)
    assert setup.session.sent_texts == [texts.HELP]
    assert setup.session.sent[0].chat_id == 1_000_001


async def test_справка_перечисляет_все_команды_spec() -> None:
    for name in ("status", "pause", "resume", "interval", "test", "last", "relogin", "cleanup"):
        assert f"/{name}" in texts.HELP
    assert "/menu" in texts.HELP and "ок" in texts.HELP


# --- /status ---


async def test_status_присылает_текст_состояния() -> None:
    setup = await make_setup()
    await setup.owner.say("/status")
    (text,) = setup.session.sent_texts
    assert text.startswith("Состояние: работает")
    assert "Размер папки: 5.0 МБ" in text and "Журнал на рабочем столе: 2.0 КБ" in text


async def test_status_с_упоминанием_этого_бота_работает_с_чужим_нет() -> None:
    setup = await make_setup()
    await setup.owner.say("/status@rcbot_test")
    await setup.owner.say("/status@other_bot")
    assert len(setup.session.sent_texts) == 1


async def test_неизвестная_команда_и_обычный_текст_молчат_и_ничего_не_меняют() -> None:
    setup = await make_setup()
    before = setup.rig.runtime.base_interval_sec
    for text in ("/unknown", "/", "привет", "/status_all"):
        await setup.owner.say(text)
    assert setup.session.sent_texts == []
    assert setup.rig.runtime.base_interval_sec == before


# --- /pause ---


async def test_pause_без_числа_до_resume() -> None:
    setup = await make_setup()
    await setup.owner.say("/pause")
    assert setup.session.sent_texts == [texts.PAUSED_FOREVER]
    assert setup.rig.runtime.paused(setup.rig.clock.now())
    assert setup.rig.runtime.paused_until - setup.rig.clock.now() > timedelta(days=365)


async def test_pause_с_числом_на_эти_минуты_и_время_в_часовом_поясе_владельца() -> None:
    setup = await make_setup()
    await setup.owner.say("/pause 30")
    now = setup.rig.clock.now()
    assert setup.rig.runtime.paused_until == now + timedelta(minutes=30)
    # T0 = 15:00 UTC = 12:00 в Монтевидео (UTC−3), пауза до 12:30 того же дня
    assert setup.session.sent_texts == ["Пауза до 12.11 12:30. /resume — продолжить."]


@pytest.mark.parametrize("raw", ["  45  ", "045", "1"])
async def test_pause_допускает_пробелы_и_ведущие_нули(raw: str) -> None:
    setup = await make_setup()
    await setup.owner.say(f"/pause {raw}")
    assert setup.rig.runtime.paused_until == setup.rig.clock.now() + timedelta(minutes=int(raw))


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "-5",
        "+5",
        "1.5",
        "1,5",
        "30 минут",
        "1e3",
        "0x10",
        "²",  # str.isdigit() истинно, int() падает
        "٣",  # арабско-индийская цифра: не ASCII
        "０",  # полноширинный ноль
        "nan",
        "99999999999999999999999999",  # не помещается в timedelta
        "0",  # 0 минут не должен молча означать «навсегда»
        "00",
        "525601",  # больше года
    ],
)
async def test_pause_с_мусором_отвечает_подсказкой_и_не_ставит_паузу(raw: str) -> None:
    setup = await make_setup()
    await setup.owner.say(f"/pause {raw}")
    (answer,) = setup.session.sent_texts
    assert answer.startswith("Нужно число минут") and "/pause 30" in answer
    assert setup.rig.runtime.paused_until is None
    assert setup.rig.kv.data.get("paused_until", "") == ""


async def test_pause_на_год_ровно_допустима() -> None:
    setup = await make_setup()
    await setup.owner.say("/pause 525600")
    assert setup.rig.runtime.paused_until == setup.rig.clock.now() + timedelta(minutes=525_600)


async def test_pause_пишет_в_хранилище_и_будит_цикл() -> None:
    setup = await make_setup()
    setup.rig.loop._wake.clear()
    await setup.owner.say("/pause 10")
    assert setup.rig.kv.data["paused_until"] != ""
    assert setup.rig.loop._wake.is_set()


# --- /resume ---


async def test_resume_снимает_паузу() -> None:
    setup = await make_setup()
    await setup.owner.say("/pause 60")
    await setup.owner.say("/resume")
    assert setup.session.sent_texts[-1] == texts.RESUMED
    assert setup.rig.runtime.paused_until is None
    assert setup.rig.kv.data["paused_until"] == ""


async def test_resume_без_паузы_безопасен() -> None:
    setup = await make_setup()
    await setup.owner.say("/resume")
    assert setup.session.sent_texts == [texts.RESUMED]


# --- /interval ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("20", 20.0), ("20,5", 20.5), ("20.5", 20.5), ("10", 10.0), ("10.0", 10.0), (" 45 ", 45.0)],
)
async def test_interval_принимает_допустимые_значения(raw: str, expected: float) -> None:
    setup = await make_setup()
    await setup.owner.say(f"/interval {raw}")
    assert setup.rig.runtime.base_interval_sec == expected
    assert setup.session.sent_texts == [texts.INTERVAL_SET.format(value=expected)]
    assert setup.rig.kv.data["poll_interval_sec"] == f"{expected:g}"


@pytest.mark.parametrize(
    "raw",
    [
        "9",
        "9.99",
        "9,99",
        "0",
        "-1",
        "-20",
        "abc",
        "",
        "   ",
        "20 с",
        "nan",
        "NaN",
        "-nan",
        "inf",
        "-inf",
        "infinity",
        "1e",
    ],
)
async def test_interval_ниже_минимума_и_мусор_отклоняются_без_изменений(raw: str) -> None:
    """Критерий приёмки 9: меньше 10 с задать нельзя; «nan» и «inf» — тоже не числа секунд."""
    setup = await make_setup()
    before = setup.rig.runtime.base_interval_sec
    await setup.owner.say(f"/interval {raw}")
    assert setup.session.sent_texts == [texts.INTERVAL_BAD]
    assert setup.rig.runtime.base_interval_sec == before
    assert math.isfinite(setup.rig.runtime.base_interval_sec)


@pytest.mark.parametrize("raw", ["1e9", "99999", "3601", "1e300"])
async def test_interval_огромное_значение_ограничивается_максимумом(raw: str) -> None:
    setup = await make_setup()
    await setup.owner.say(f"/interval {raw}")
    assert setup.rig.runtime.base_interval_sec == MAX_POLL_INTERVAL_SEC
    assert setup.session.sent_texts == [texts.INTERVAL_SET.format(value=MAX_POLL_INTERVAL_SEC)]


async def test_interval_без_аргумента_не_падает() -> None:
    setup = await make_setup()
    await setup.owner.say("/interval")
    assert setup.session.sent_texts == [texts.INTERVAL_BAD]


async def test_interval_никогда_не_опускается_ниже_минимума_после_любой_последовательности() -> (
    None
):
    setup = await make_setup()
    for raw in ("5", "20", "abc", "9.9", "nan", "10", "-1", "inf", "1e9", "0"):
        await setup.owner.say(f"/interval {raw}")
        value = setup.rig.runtime.base_interval_sec
        assert math.isfinite(value) and MIN_POLL_INTERVAL_SEC <= value <= MAX_POLL_INTERVAL_SEC


# --- /test ---


async def test_test_шлёт_во_все_каналы_и_показывает_итог() -> None:
    setup = await make_setup()
    setup.rig.notifiers[Channel.CALL].ok = False
    setup.rig.notifiers[Channel.CALL].error = "HTTP 500"
    await setup.owner.say("/test")
    first, second = setup.session.sent_texts
    assert first == "Отправляю тест во все каналы…"
    assert "✅ Telegram" in second and "✅ ntfy" in second
    assert "❌ звонок" in second and "HTTP 500" in second
    assert all(len(n.sent) == 1 for n in setup.rig.notifiers.values())


async def test_test_работает_и_с_выключенным_каналом_но_не_создаёт_инцидент() -> None:
    setup = await make_setup()
    setup.rig.runtime.channels_off.add(Channel.NTFY)
    await setup.owner.say("/test")
    assert len(setup.rig.notifiers[Channel.NTFY].sent) == 1  # тест проверяет и выключенный канал
    assert setup.rig.incidents.incidents == {}
    assert setup.rig.alerts.repeating() is False


# --- /last ---


async def test_last_без_инцидентов() -> None:
    setup = await make_setup()
    await setup.owner.say("/last")
    assert setup.session.sent_texts == ["Инцидентов ещё не было."]


async def test_last_показывает_открытый_инцидент_с_датами() -> None:
    setup = await make_setup()
    await appear(setup)
    await setup.owner.say("/last")
    (text,) = setup.session.sent_texts
    assert "№1" in text and "открыт" in text and "23.02, 24.02" in text


async def test_last_не_больше_пяти() -> None:
    setup = await make_setup()
    for _ in range(7):
        await setup.rig.incidents.open(1, T0, slots(23))
    await setup.owner.say("/last")
    assert setup.session.sent_texts[0].count("№") == 5


# --- /relogin ---


async def test_relogin_успех() -> None:
    setup = await make_setup()
    await setup.owner.say("/relogin")
    assert setup.session.sent_texts == ["Вхожу заново…", texts.RELOGIN_OK]
    assert setup.rig.auth.logins == 1 and setup.rig.loop._wake.is_set()


@pytest.mark.parametrize(
    "error",
    [LoginFailed("страница входа изменилась"), ReloginBudgetExceeded("уже 6 входов за час")],
)
async def test_relogin_ошибка_показывается_владельцу(error: Exception) -> None:
    setup = await make_setup(relogin_failures=(error,))
    await setup.owner.say("/relogin")
    assert setup.session.sent_texts == ["Вхожу заново…", texts.RELOGIN_FAILED.format(error=error)]


async def test_relogin_после_тревоги_отложен_d5_и_сообщает_об_этом() -> None:
    setup = await make_setup()
    setup.rig.session.quiet_after_alert()
    await setup.owner.say("/relogin")
    assert setup.rig.auth.logins == 0
    assert "отложен" in setup.session.sent_texts[-1]
    assert isinstance(ReloginDeferred("x"), Exception)


# --- /cleanup ---


async def test_cleanup_ничего_не_нашёл() -> None:
    setup = await make_setup()
    await setup.owner.say("/cleanup")
    assert setup.session.sent_texts == ["Очищать нечего."]
    assert setup.rig.cleanup_calls == [1]


async def test_cleanup_сообщает_что_удалено() -> None:
    setup = await make_setup(cleanup={"снимки": 3, "логи": 2})
    await setup.owner.say("/cleanup")
    assert setup.session.sent_texts == ["Удалено: снимки — 3, логи — 2"]


# --- /menu ---


async def test_menu_открывает_главное_окно_с_кнопками() -> None:
    setup = await make_setup()
    await setup.owner.say("/menu")
    (message,) = setup.session.sent
    assert message.text == "Меню rcbot"
    labels = [b.text for row in message.reply_markup.inline_keyboard for b in row]  # type: ignore[union-attr]
    assert labels == [
        "📊 Статус",
        "🔎 Сканирование",
        "🔔 Уведомления",
        "🛠 Обслуживание",
        "✖️ Закрыть",
    ]


async def test_повторный_menu_начинает_меню_заново() -> None:
    setup = await make_setup()
    await setup.owner.say("/menu")
    await setup.owner.say("/menu")
    assert [m.text for m in setup.session.sent] == ["Меню rcbot", "Меню rcbot"]
