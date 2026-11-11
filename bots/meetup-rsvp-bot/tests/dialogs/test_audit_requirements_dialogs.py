"""Требования к меню админа, найденные аудитом (код подгоняется под тесты)."""

from __future__ import annotations

from meetup_bot.db.models import Event
from meetup_bot.renderer.post import CAPTION_LIMIT, visible_length
from meetup_bot.services.settings_schema import MESSAGE_MAX_LEN
from tests.dialogs.test_wizard import _photo_update
from tests.helpers import OWNER_ID, App, person
from tests.tg_fake import Harness, command_update, text_update

MESSAGE_LIMIT = 4096


def _inline(tg: Harness) -> int:
    ids = [m for m, mk in tg.session.last_markup.items() if "inline_keyboard" in mk]
    return max(ids)


async def _say(tg: Harness, text: str) -> None:
    await tg.feed(text_update(OWNER_ID, text))


async def _capacity(app: App, event_id: int) -> int:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
    assert event is not None
    return event.capacity


async def _open_capacity(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    await tg.click(OWNER_ID, mid, "занято")
    await tg.click(OWNER_ID, mid, "Редактировать")
    await tg.click(OWNER_ID, mid, "Лимит мест")


# --- лимит мест -----------------------------------------------------------------------------


async def test_уменьшение_лимита_требует_подтверждения(app: App, tg: Harness) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=10)).event_id
    await _open_capacity(tg)

    await _say(tg, "6")
    assert await _capacity(app, event_id) == 10
    assert any("Подтвердить" in label for label, _ in tg.session.buttons(_inline(tg)))

    await tg.click(OWNER_ID, _inline(tg), "Подтвердить")
    assert await _capacity(app, event_id) == 6


async def test_отказ_от_уменьшения_лимита_ничего_не_меняет(app: App, tg: Harness) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=10)).event_id
    await _open_capacity(tg)
    await _say(tg, "6")
    await tg.click(OWNER_ID, _inline(tg), "Отмена")
    assert await _capacity(app, event_id) == 10


async def test_увеличение_лимита_без_подтверждения(app: App, tg: Harness) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=10)).event_id
    await _open_capacity(tg)
    await _say(tg, "15")
    assert await _capacity(app, event_id) == 15


async def test_уменьшение_ниже_занятых_отклоняется_до_подтверждения(app: App, tg: Harness) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=10)).event_id
    for n in range(4):
        await app.services.registration.register(event_id, person(n))
    await _open_capacity(tg)
    await _say(tg, "2")
    assert any("ниже числа занятых" in d.get("text", "") for d in tg.session.of("SendMessage"))
    assert await _capacity(app, event_id) == 10


# --- предпросмотр мастера ------------------------------------------------------------------


async def test_предпросмотр_с_фото_не_превышает_лимит_подписи(app: App, tg: Harness) -> None:
    await _to_photo_with_long_description(tg)
    await tg.feed(_photo_update(OWNER_ID, "photo-1"))
    await _say(tg, "12")

    captions = [d["caption"] for d in tg.session.calls_with("caption")]
    assert captions, "предпросмотр с фото должен уйти подписью"
    assert visible_length(captions[-1]) <= CAPTION_LIMIT


async def _to_photo_with_long_description(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    await tg.click(OWNER_ID, mid, "Новая встреча")
    await tg.click(OWNER_ID, _inline(tg), "Ближайшая")
    await tg.click(OWNER_ID, _inline(tg), "18:00")
    await _say(tg, "Кафе")
    await _say(tg, "Очень длинное описание встречи. " * 40)


# --- окно текста в настройках --------------------------------------------------------------


async def test_окно_длинного_текста_сообщения_укладывается_в_лимит(app: App, tg: Harness) -> None:
    long_text = "Напоминаем о встрече {date}. " + "очень длинный текст " * 170
    await app.services.settings.set("text.reminder", long_text, OWNER_ID)
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    for label in ("Настройки", "Тексты сообщений", "Напоминание"):
        await tg.click(OWNER_ID, mid, label)

    texts = [d["text"] for d in tg.session.of("EditMessageText") + tg.session.of("SendMessage")]
    assert len(texts[-1]) <= MESSAGE_LIMIT
    assert any("Сбросить" in label for label, _ in tg.session.buttons(mid))


async def test_слишком_длинный_текст_сообщения_не_сохраняется(app: App) -> None:
    from meetup_bot.services.settings_schema import SettingsValidationError

    try:
        await app.services.settings.set("text.reminder", "x" * (MESSAGE_MAX_LEN + 1), OWNER_ID)
    except SettingsValidationError:
        pass
    else:
        raise AssertionError("слишком длинный текст должен отклоняться")
