"""Меню aiogram-dialog (SPEC 2.5): окна, переходы, выбор только из готовых значений, ответы."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import InlineKeyboardMarkup, Message

from rcbot.bot import texts
from rcbot.bot.dialogs.menu import INTERVALS, MONTHS, PAUSES
from rcbot.config.settings import MIN_POLL_INTERVAL_SEC
from rcbot.domain.errors import LoginFailed
from rcbot.domain.models import Channel
from tests.fakes import T0
from tests.unit.bot.harness import Setup, make_setup, snapshot_of_state


class Screen:
    """Что владелец видит сейчас: последний текст и клавиатура меню, нажатие по подписи кнопки."""

    def __init__(self, setup: Setup) -> None:
        self.setup = setup

    def _last(self) -> SendMessage | EditMessageText:
        calls = [
            c.method
            for c in self.setup.session.calls
            if isinstance(c.method, (SendMessage, EditMessageText))
        ]
        return calls[-1]

    @property
    def text(self) -> str:
        return self._last().text

    @property
    def markup(self) -> InlineKeyboardMarkup:
        markup = self._last().reply_markup
        assert isinstance(markup, InlineKeyboardMarkup)
        return markup

    @property
    def labels(self) -> list[str]:
        return [b.text for row in self.markup.inline_keyboard for b in row]

    def data_of(self, label: str) -> str:
        found = [
            b.callback_data
            for row in self.markup.inline_keyboard
            for b in row
            if b.text == label and b.callback_data
        ]
        assert len(found) == 1, f"кнопка «{label}» не найдена среди {self.labels}"
        return found[0]

    def message(self) -> Message:
        return Message(
            message_id=self.setup.session.last_message_id,
            date=1_700_000_000,  # type: ignore[arg-type]
            chat=self.setup.owner.chat,
            text=self.text,
            reply_markup=self.markup,
        )

    async def _press(self, data: str) -> list[AnswerCallbackQuery]:
        """Ответы на это нажатие. Пустой ответ добавляет сама aiogram-dialog после обработчика."""
        before = len(self.setup.session.answers)
        await self.setup.owner.press(data, on=self.message())
        new = self.setup.session.answers[before:]
        assert new, "на каждое нажатие должен быть ответ, иначе кнопка «крутится»"
        return [a for a in new if a.text is not None]

    async def click(self, label: str) -> list[AnswerCallbackQuery]:
        return await self._press(self.data_of(label))

    async def forge(self, label: str, new_item: str) -> list[AnswerCallbackQuery]:
        """Подделанное нажатие: данные настоящей кнопки, но со своим значением в конце."""
        head, _, _ = self.data_of(label).rpartition(":")
        return await self._press(f"{head}:{new_item}")


async def open_menu() -> tuple[Setup, Screen]:
    setup = await make_setup()
    await setup.owner.say("/menu")
    return setup, Screen(setup)


# --- главное окно и переходы ---


async def test_главное_окно() -> None:
    _, screen = await open_menu()
    assert screen.text == "Меню rcbot"
    assert screen.labels == [
        "📊 Статус",
        "🔎 Сканирование",
        "🔔 Уведомления",
        "🛠 Обслуживание",
        "✖️ Закрыть",
    ]


@pytest.mark.parametrize(
    ("button", "marker"),
    [
        ("📊 Статус", "Состояние: работает"),
        ("🔎 Сканирование", "Интервал: 20 с"),
        ("🔔 Уведомления", "Каналы"),
        ("🛠 Обслуживание", "Размер папки"),
    ],
)
async def test_каждый_раздел_открывается_и_возвращается_назад(button: str, marker: str) -> None:
    _, screen = await open_menu()
    await screen.click(button)
    assert marker in screen.text
    assert "⬅️ Назад" in screen.labels
    await screen.click("⬅️ Назад")
    assert screen.text == "Меню rcbot"


async def test_переход_отвечает_на_нажатие() -> None:
    """Иначе у кнопки в Telegram бесконечно крутятся «часики»."""
    _, screen = await open_menu()
    answers = await screen.click("📊 Статус")
    assert answers == []  # текста нет, но пустой ответ ушёл (см. _press)


# --- статус ---


async def test_окно_статуса_показывает_тот_же_текст_что_команда_status() -> None:
    setup, screen = await open_menu()
    await screen.click("📊 Статус")
    expected = texts.status_text(await setup.rig.controller.status(), setup.dp.workflow_data["tz"])
    assert screen.text == expected


async def test_обновить_показывает_свежие_данные() -> None:
    setup, screen = await open_menu()
    await screen.click("📊 Статус")
    assert "Состояние: работает" in screen.text and "Пауза до" not in screen.text
    await setup.rig.controller.pause(30)
    await screen.click("🔄 Обновить")
    assert "Пауза до 12.11 12:30" in screen.text
    assert "🔄 Обновить" in screen.labels


# --- сканирование ---


def test_готовые_значения_интервала_не_ниже_минимума() -> None:
    """Критерий приёмки 9: из меню меньше 10 с задать нельзя."""
    assert all(float(v) >= MIN_POLL_INTERVAL_SEC for v in INTERVALS)
    assert list(INTERVALS) == sorted(INTERVALS, key=float)


async def test_окно_сканирования_показывает_текущие_настройки() -> None:
    _, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    assert "Интервал: 20 с" in screen.text and "Месяцев вперёд: 6" in screen.text
    assert "Пауза: нет" in screen.text
    assert set(INTERVALS) <= set(screen.labels) and set(MONTHS) <= set(screen.labels)
    assert {label for _, label in PAUSES} <= set(screen.labels)


@pytest.mark.parametrize("value", INTERVALS)
async def test_выбор_интервала_из_готовых_значений(value: str) -> None:
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    answers = await screen.click(value)
    assert setup.rig.runtime.base_interval_sec == float(value)
    assert [a.text for a in answers] == [f"Интервал {value} с"]
    assert setup.rig.kv.data["poll_interval_sec"] == value


@pytest.mark.parametrize("forged", ["5", "0", "-20", "9", "nan", "inf", "abc", "1e9", "15"])
async def test_подделанное_значение_интервала_не_применяется(forged: str) -> None:
    """Кнопка с чужим значением (подмена callback_data) не должна обойти «меньше 10 с нельзя»."""
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    before = snapshot_of_state(setup.rig)
    await screen.forge("20", forged)
    assert snapshot_of_state(setup.rig) == before


@pytest.mark.parametrize("value", MONTHS)
async def test_выбор_месяцев(value: str) -> None:
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    answers = await screen.click(value)
    assert setup.rig.runtime.months_ahead == int(value)
    assert [a.text for a in answers] == [f"Месяцев вперёд: {value}"]


@pytest.mark.parametrize("forged", ["0", "13", "-1", "99", "x", "7"])
async def test_подделанное_число_месяцев_не_применяется(forged: str) -> None:
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    before = snapshot_of_state(setup.rig)
    await screen.forge("6", forged)
    assert snapshot_of_state(setup.rig) == before


@pytest.mark.parametrize(("minutes", "label"), PAUSES[:3])
async def test_пауза_на_выбранное_время(minutes: str, label: str) -> None:
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    answers = await screen.click(label)
    assert setup.rig.runtime.paused_until == T0 + timedelta(minutes=int(minutes))
    assert [a.text for a in answers] == ["Пауза включена"]


async def test_пауза_до_resume() -> None:
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    await screen.click("до /resume")
    assert setup.rig.runtime.paused_until is not None
    assert setup.rig.runtime.paused_until - T0 > timedelta(days=365)


async def test_после_паузы_окно_показывает_паузу_и_продолжить_её_снимает() -> None:
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    await screen.click("1 ч")
    assert "Пауза: да" in screen.text
    answers = await screen.click("▶️ Продолжить")
    assert setup.rig.runtime.paused_until is None
    assert [a.text for a in answers] == [texts.RESUMED]


# --- уведомления ---


async def test_окно_каналов_показывает_включённые_и_выключенные() -> None:
    setup, screen = await open_menu()
    setup.rig.runtime.channels_off.add(Channel.NTFY)
    await screen.click("🔔 Уведомления")
    assert {"🔔 Telegram", "🔕 ntfy", "🔔 звонок"} <= set(screen.labels)
    assert {"Тест: Telegram", "Тест: ntfy", "Тест: звонок"} <= set(screen.labels)


@pytest.mark.parametrize(
    ("channel", "label"),
    [(Channel.TELEGRAM, "Telegram"), (Channel.NTFY, "ntfy"), (Channel.CALL, "звонок")],
)
async def test_переключение_канала_туда_и_обратно(channel: Channel, label: str) -> None:
    setup, screen = await open_menu()
    await screen.click("🔔 Уведомления")
    off = await screen.click(f"🔔 {label}")
    assert channel in setup.rig.runtime.channels_off
    assert [a.text for a in off] == ["Канал выключен"]
    assert f"🔕 {label}" in screen.labels
    on = await screen.click(f"🔕 {label}")
    assert channel not in setup.rig.runtime.channels_off
    assert [a.text for a in on] == ["Канал включён"]
    assert setup.rig.kv.data["channels_off"] == ""


async def test_выключение_сохраняется_в_хранилище() -> None:
    setup, screen = await open_menu()
    await screen.click("🔔 Уведомления")
    await screen.click("🔔 звонок")
    assert setup.rig.kv.data["channels_off"] == "call"


@pytest.mark.parametrize(
    ("channel", "label"),
    [(Channel.TELEGRAM, "Telegram"), (Channel.NTFY, "ntfy"), (Channel.CALL, "звонок")],
)
async def test_тест_канала_шлёт_только_в_этот_канал(channel: Channel, label: str) -> None:
    setup, screen = await open_menu()
    await screen.click("🔔 Уведомления")
    (answer,) = await screen.click(f"Тест: {label}")
    for other, notifier in setup.rig.notifiers.items():
        assert len(notifier.sent) == (1 if other is channel else 0)
    assert answer.show_alert is True
    assert answer.text == f"✅ {label}: 1 мс"


async def test_тест_выключенного_канала_всё_равно_отправляется() -> None:
    setup, screen = await open_menu()
    setup.rig.runtime.channels_off.add(Channel.NTFY)
    await screen.click("🔔 Уведомления")
    await screen.click("Тест: ntfy")
    assert len(setup.rig.notifiers[Channel.NTFY].sent) == 1


async def test_тест_с_ошибкой_показывает_причину() -> None:
    setup, screen = await open_menu()
    setup.rig.notifiers[Channel.CALL].ok = False
    setup.rig.notifiers[Channel.CALL].error = "HTTP 500"
    await screen.click("🔔 Уведомления")
    (answer,) = await screen.click("Тест: звонок")
    assert answer.text == "❌ звонок: 1 мс — HTTP 500"


async def test_длинный_итог_теста_укладывается_в_лимит_200_символов_telegram() -> None:
    setup, screen = await open_menu()
    setup.rig.notifiers[Channel.CALL].ok = False
    setup.rig.notifiers[Channel.CALL].error = "очень длинная ошибка " * 40
    await screen.click("🔔 Уведомления")
    (answer,) = await screen.click("Тест: звонок")
    assert answer.show_alert is True
    assert answer.text is not None and 0 < len(answer.text) <= 200


async def test_упавший_канал_при_тесте_не_роняет_меню() -> None:
    setup, screen = await open_menu()
    setup.rig.notifiers[Channel.NTFY].raises = RuntimeError("канал упал")
    await screen.click("🔔 Уведомления")
    (answer,) = await screen.click("Тест: ntfy")
    assert answer.text is not None and "❌ ntfy" in answer.text and "RuntimeError" in answer.text


async def test_подделанный_канал_не_применяется() -> None:
    setup, screen = await open_menu()
    await screen.click("🔔 Уведомления")
    before = snapshot_of_state(setup.rig)
    await screen.forge("🔔 Telegram", "sms")
    await screen.forge("Тест: Telegram", "sms")
    assert snapshot_of_state(setup.rig) == before


# --- обслуживание ---


async def test_окно_обслуживания_размер_и_ошибки() -> None:
    from rcbot.domain.models import AppEvent

    setup, screen = await open_menu()
    await setup.rig.event_log.write(AppEvent(T0, "WARNING", "poll", "сайт ответил 503"))
    await screen.click("🛠 Обслуживание")
    assert "Размер папки: 5.0 МБ" in screen.text
    assert "12.11 12:00 WARNING: сайт ответил 503" in screen.text


async def test_окно_обслуживания_без_ошибок() -> None:
    _, screen = await open_menu()
    await screen.click("🛠 Обслуживание")
    assert "Ошибок нет." in screen.text


async def test_войти_заново_успех() -> None:
    setup, screen = await open_menu()
    await screen.click("🛠 Обслуживание")
    answers = await screen.click("🔑 Войти заново")
    assert [a.text for a in answers] == ["Вхожу заново…"]
    assert setup.rig.auth.logins == 1
    assert setup.session.sent_texts[-1] == texts.RELOGIN_OK


async def test_войти_заново_ошибка() -> None:
    setup = await make_setup(relogin_failures=(LoginFailed("страница входа изменилась"),))
    await setup.owner.say("/menu")
    screen = Screen(setup)
    await screen.click("🛠 Обслуживание")
    await screen.click("🔑 Войти заново")
    assert setup.session.sent_texts[-1] == "Вход не удался: страница входа изменилась"


async def test_очистить_сейчас_показывает_итог_во_всплывающем_окне() -> None:
    setup = await make_setup(cleanup={"снимки": 3})
    await setup.owner.say("/menu")
    screen = Screen(setup)
    await screen.click("🛠 Обслуживание")
    (answer,) = await screen.click("🧹 Очистить сейчас")
    assert answer.show_alert is True and answer.text == "Удалено: снимки — 3"
    assert setup.rig.cleanup_calls == [1]


async def test_очистить_сейчас_длинный_итог_укладывается_в_200() -> None:
    setup = await make_setup(cleanup={f"каталог-{i}": i for i in range(60)})
    await setup.owner.say("/menu")
    screen = Screen(setup)
    await screen.click("🛠 Обслуживание")
    (answer,) = await screen.click("🧹 Очистить сейчас")
    assert answer.text is not None and len(answer.text) <= 200


# --- закрытие ---


async def test_закрыть_завершает_меню_и_меню_открывается_снова() -> None:
    setup, screen = await open_menu()
    await screen.click("✖️ Закрыть")
    await setup.owner.say("/menu")
    assert Screen(setup).text == "Меню rcbot"


async def test_после_закрытия_старые_кнопки_ничего_не_меняют() -> None:
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    stale = screen.message()
    interval_data = screen.data_of("45")
    await screen.click("⬅️ Назад")
    await screen.click("✖️ Закрыть")
    before = snapshot_of_state(setup.rig)
    await setup.owner.press(interval_data, on=stale)
    assert snapshot_of_state(setup.rig) == before


async def test_во_всех_окнах_текст_и_клавиатура_в_лимитах_telegram() -> None:
    _, screen = await open_menu()
    for section in ("📊 Статус", "🔎 Сканирование", "🔔 Уведомления", "🛠 Обслуживание"):
        await screen.click(section)
        assert len(screen.text) <= 4096
        for row in screen.markup.inline_keyboard:
            for button in row:
                assert button.callback_data and len(button.callback_data.encode()) <= 64
        await screen.click("⬅️ Назад")


async def test_кнопка_меню_после_перезапуска_бота_отвечает_что_меню_устарело() -> None:
    """Состояние диалогов хранится в памяти: после перезапуска старые кнопки неизвестны боту."""
    setup, screen = await open_menu()
    await screen.click("🔎 Сканирование")
    stale_message = screen.message()
    stale_data = screen.data_of("45")
    restarted = await make_setup()
    before = snapshot_of_state(restarted.rig)
    await restarted.owner.press(stale_data, on=stale_message)
    assert [a.text for a in restarted.session.answers] == ["Меню устарело: откройте заново /menu"]
    assert snapshot_of_state(restarted.rig) == before
    assert setup.rig.runtime.base_interval_sec == 20.0
