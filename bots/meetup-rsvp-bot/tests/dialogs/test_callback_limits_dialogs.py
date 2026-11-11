"""Лимит Telegram на `callback_data` (64 байта) во всех окнах меню админа этой ветки.

Строгий фейк Telegram отклоняет клавиатуру с длинными данными, а глобальный обработчик
ошибок это проглатывает, поэтому проверяем результат прямо: окно нарисовано, кнопки есть и
каждая укладывается в лимит.
"""

import pytest

from meetup_bot.dialogs.settings_view import GROUPS
from meetup_bot.services.settings_schema import SCHEMA
from meetup_bot.texts.defaults import DEFAULT_TEXTS
from tests.helpers import OWNER_ID, App, person
from tests.tg_fake import (
    CALLBACK_DATA_LIMIT,
    Harness,
    callback_update,
    command_update,
    text_update,
)

LONGEST_SETTING_BUTTON = "setting_pick:registration_close_minutes_after_start"


def _inline(tg: Harness) -> int:
    return max(m for m, mk in tg.session.last_markup.items() if "inline_keyboard" in mk)


def _assert_fits(tg: Harness, expected_label: str) -> None:
    buttons = tg.session.buttons(_inline(tg))
    assert any(expected_label in text for text, _ in buttons), (expected_label, buttons)
    too_long = [data for _, data in buttons if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT]
    assert not too_long, too_long


async def _press(tg: Harness, label: str) -> None:
    await tg.click(OWNER_ID, _inline(tg), label)


async def _press_data(tg: Harness, suffix: str) -> None:
    """Нажимает кнопку по хвосту её данных (ключ элемента списка), а не по подписи."""
    mid = _inline(tg)
    data = next(d for _, d in tg.session.buttons(mid) if d.endswith(suffix))
    markup = tg.session.last_markup[mid]
    await tg.feed(callback_update(OWNER_ID, data, message_id=mid, reply_markup=markup))


async def _menu(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))


async def _known_ban(app: App) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=5)).event_id
    await app.services.registration.register(event_id, person(1))
    await app.services.bans.ban(OWNER_ID, person(1).id)


# ---- меню ----------------------------------------------------------------------------------


async def test_главное_меню_и_инструкция(tg: Harness) -> None:
    await _menu(tg)
    _assert_fits(tg, "Админы")
    await _press(tg, "Как разослать пост")
    _assert_fits(tg, "В меню")


# ---- чёрный список ---------------------------------------------------------------------------


async def test_окна_чёрного_списка(app: App, tg: Harness) -> None:
    await _known_ban(app)
    await _menu(tg)
    await _press(tg, "Чёрный список")
    _assert_fits(tg, "Забанить")
    await _press(tg, "Участник1")
    _assert_fits(tg, "История")
    await _press(tg, "История")
    _assert_fits(tg, "Назад")
    await _press(tg, "Назад")
    await _press(tg, "Разбанить")
    _assert_fits(tg, "Да, разбанить")


# ---- админы ----------------------------------------------------------------------------------


async def test_окна_админов(tg: Harness) -> None:
    await _menu(tg)
    await _press(tg, "Админы")
    _assert_fits(tg, "Добавить админа")
    await _press(tg, "Админ (@admin2)")
    _assert_fits(tg, "Удалить из админов")
    await _press(tg, "Удалить из админов")
    _assert_fits(tg, "Да, удалить")


# ---- выбор пользователя ------------------------------------------------------------------------


async def test_окно_списка_известных_с_листанием(app: App, tg: Harness) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=50)).event_id
    for n in range(1, 11):
        await app.services.registration.register(event_id, person(n))
    await _menu(tg)
    await _press(tg, "Чёрный список")
    await _press(tg, "Забанить")
    keyboard = tg.session.of("SendMessage")[-1]["reply_markup"]["keyboard"]
    known_label = next(b["text"] for row in keyboard for b in row if "Из списка" in b["text"])
    await tg.feed(text_update(OWNER_ID, known_label))
    _assert_fits(tg, "▶")


# ---- настройки ---------------------------------------------------------------------------------


async def _open_settings(tg: Harness) -> None:
    await _menu(tg)
    await _press(tg, "Настройки")


async def test_корень_настроек_и_часовой_пояс(tg: Harness) -> None:
    await _open_settings(tg)
    _assert_fits(tg, "Часовой пояс")
    await _press(tg, "Часовой пояс")
    _assert_fits(tg, "Назад")


_GROUP_BUTTONS = {
    "defaults": "Встречи по умолчанию",
    "schedule": "Расписание",
    "rules": "Правила",
}


@pytest.mark.parametrize(
    ("group", "key"),
    [(group, key) for group, (_, _, keys) in GROUPS.items() for key in keys],
    ids=lambda value: str(value),
)
async def test_каждое_окно_редактирования_настройки(tg: Harness, group: str, key: str) -> None:
    await _open_settings(tg)
    await _press(tg, _GROUP_BUTTONS[group])
    _assert_fits(tg, "Назад")
    await _press_data(tg, f"setting_pick:{key}")
    assert key in SCHEMA
    _assert_fits(tg, "Сбросить по умолчанию")


async def test_самая_длинная_кнопка_настроек_проходит(tg: Harness) -> None:
    await _open_settings(tg)
    await _press(tg, "Расписание")
    mid = _inline(tg)
    data = next(d for _, d in tg.session.buttons(mid) if d.endswith(LONGEST_SETTING_BUTTON))
    assert len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT


@pytest.mark.parametrize("key", list(DEFAULT_TEXTS))
async def test_каждое_окно_редактирования_текста(tg: Harness, key: str) -> None:
    await _open_settings(tg)
    await _press(tg, "Тексты сообщений")
    _assert_fits(tg, "Назад")
    await _press_data(tg, f"text_pick:{key}")
    _assert_fits(tg, "Сбросить по умолчанию")
