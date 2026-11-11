"""Окна встреч, участников и мастера укладываются в лимиты Bot API.

Фейковый Telegram сам отклоняет нарушения (callback_data > 64 байт, текст > 4096, подпись > 1024);
здесь он подслушивается, чтобы отказ нельзя было принять за «окно просто не обновилось».
"""

from collections.abc import Callable
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest

from meetup_bot.services.settings_schema import PLACE_MAX_LEN
from tests.helpers import OWNER_ID, App, person
from tests.tg_fake import CALLBACK_DATA_LIMIT, Harness, command_update, text_update

type Step = tuple[str, str]

LONG_NAME = "Я" * 90


def _press(label: str) -> Step:
    return ("press", label)


def _say(text: str) -> Step:
    return ("say", text)


def _inline(tg: Harness) -> int:
    return max(m for m, markup in tg.session.last_markup.items() if "inline_keyboard" in markup)


def _screen(tg: Harness) -> str:
    for name, data in reversed(tg.session.calls):
        if name in {"SendMessage", "EditMessageText", "SendPhoto", "EditMessageCaption"}:
            text = data.get("text") or data.get("caption")
            if text:
                return str(text)
    return ""


def _spy_on_limits(tg: Harness) -> list[TelegramBadRequest]:
    """Запоминает отказы фейка по лимитам: нарушение в диалоге — настоящая ошибка."""
    rejected: list[TelegramBadRequest] = []
    original: Callable[..., None] = tg.session._check_limits

    def spy(*args: Any, **kwargs: Any) -> None:
        try:
            original(*args, **kwargs)
        except TelegramBadRequest as error:
            rejected.append(error)
            raise

    tg.session._check_limits = spy  # type: ignore[method-assign]
    return rejected


_OPEN_CARD: list[Step] = [_press("Встречи"), _press("занято")]
_OPEN_EDIT = [*_OPEN_CARD, _press("Редактировать")]
_OPEN_PARTICIPANT = [*_OPEN_CARD, _press("Участники"), _press("1. ")]
_WIZARD_TO_PREVIEW = [
    _press("Встречи"),
    _press("Новая встреча"),
    _press("Ближайшая"),
    _press("18:00"),
    _say("Кафе «Утро» | https://maps.example.com/xyz"),
    _say("Приходите"),
    _press("Пропустить"),
]

# (название сценария, шаги, фрагмент текста последнего окна)
SCENARIOS: list[tuple[str, list[Step], str]] = [
    ("список", [_press("Встречи")], "Встречи"),
    ("карточка", _OPEN_CARD, "Статус: запись открыта"),
    ("предпросмотр", [*_OPEN_CARD, _press("Предпросмотр")], "П" * 20),
    ("участники", [*_OPEN_CARD, _press("Участники")], "Занято мест: 3 из 5"),
    ("участник", _OPEN_PARTICIPANT, "Статус: записан(а)"),
    ("удаление", [*_OPEN_PARTICIPANT, _press("Удалить из встречи")], "Удалить участника"),
    ("бан", [*_OPEN_PARTICIPANT, _press("Забанить")], "Забанить"),
    (
        "добавление",
        [*_OPEN_CARD, _press("Участники"), _press("Добавить участника")],
        "Кого записать?",
    ),
    ("отмена встречи", [*_OPEN_CARD, _press("Отменить встречу")], "причину отмены"),
    (
        "подтверждение отмены",
        [*_OPEN_CARD, _press("Отменить встречу"), _press("Без причины")],
        "Отменить встречу?",
    ),
    ("меню правок", _OPEN_EDIT, "Что изменить?"),
    ("описание", [*_OPEN_EDIT, _press("Описание")], "Описание"),
    ("фото", [*_OPEN_EDIT, _press("Фото")], "Фото"),
    ("место", [*_OPEN_EDIT, _press("Место")], "Место"),
    ("ссылка", [*_OPEN_EDIT, _press("Ссылка на место")], "Ссылка на место"),
    ("дата", [*_OPEN_EDIT, _press("Дата и время")], "Дата и время"),
    ("лимит", [*_OPEN_EDIT, _press("Лимит мест")], "Лимит мест"),
    (
        "подтверждение лимита",
        [*_OPEN_EDIT, _press("Лимит мест"), _say("4")],
        "Уменьшить лимит мест с 5 до 4?",
    ),
    ("моменты", [*_OPEN_EDIT, _press("Моменты")], "Выберите, что изменить."),
    (
        "выбранный момент",
        [*_OPEN_EDIT, _press("Моменты"), _press("Запрос явки")],
        "Значение для «Запрос явки»",
    ),
    ("мастер: дата", [_press("Встречи"), _press("Новая встреча")], "шаг 1 из 6"),
    (
        "мастер: время",
        [_press("Встречи"), _press("Новая встреча"), _press("Ближайшая")],
        "шаг 2 из 6",
    ),
    (
        "мастер: место",
        [*_WIZARD_TO_PREVIEW[:4]],
        "шаг 3 из 6",
    ),
    ("мастер: текст", [*_WIZARD_TO_PREVIEW[:5]], "шаг 4 из 6"),
    ("мастер: фото", [*_WIZARD_TO_PREVIEW[:6]], "шаг 5 из 6"),
    ("мастер: лимит", _WIZARD_TO_PREVIEW, "шаг 6 из 6"),
    ("мастер: предпросмотр", [*_WIZARD_TO_PREVIEW, _say("12")], "Предпросмотр"),
]


@pytest.mark.parametrize(
    ("steps", "expected"), [(s, e) for _, s, e in SCENARIOS], ids=[n for n, _, _ in SCENARIOS]
)
async def test_окно_отрисовывается_в_лимитах_bot_api(
    app: App, tg: Harness, steps: list[Step], expected: str
) -> None:
    created = await app.services.events.create_event_as_admin(
        OWNER_ID, capacity=5, place="П" * PLACE_MAX_LEN, place_url="https://maps.example.com/cafe"
    )
    for n in (1, 2):
        await app.services.registration.register(created.event_id, person(n))
    await app.services.participants.admin_add(OWNER_ID, created.event_id, manual_name=LONG_NAME)
    rejected = _spy_on_limits(tg)

    await tg.feed(command_update(OWNER_ID, "/menu"))
    for kind, arg in steps:
        if kind == "press":
            await tg.click(OWNER_ID, _inline(tg), arg)
        else:
            await tg.feed(text_update(OWNER_ID, arg))

    assert not rejected, [str(error) for error in rejected]
    assert expected in _screen(tg)
    for _label, data in tg.session.buttons(_inline(tg)):
        assert len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT
