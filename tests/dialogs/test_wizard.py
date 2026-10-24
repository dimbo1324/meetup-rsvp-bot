"""Мастер «Новая встреча»: шаги, проверки ввода, предпросмотр, создание и отмена."""

from datetime import UTC, datetime, timedelta

from aiogram.types import Message, PhotoSize, Update
from sqlalchemy import select

from meetup_bot.db.models import Event
from tests.helpers import ADMIN_ID, OWNER_ID, App
from tests.tg_fake import Harness, chat, command_update, next_id, text_update, tg_user

# 18:00 в Монтевидео (UTC-3) в ближайшую субботу
SATURDAY_18 = datetime(2026, 10, 24, 21, 0, tzinfo=UTC)


# ---- локальные помощники -------------------------------------------------------------


def _inline(tg: Harness) -> int:
    ids = [m for m, mk in tg.session.last_markup.items() if "inline_keyboard" in mk]
    return max(ids)


def _labels(tg: Harness) -> list[str]:
    return [text for text, _ in tg.session.buttons(_inline(tg))]


def _screen(tg: Harness) -> str:
    for name, data in reversed(tg.session.calls):
        if name in {"SendMessage", "EditMessageText", "SendPhoto", "EditMessageCaption"}:
            text = data.get("text") or data.get("caption")
            if text:
                return str(text)
    return ""


def _alerts(tg: Harness) -> list[str]:
    return [str(d["text"]) for d in tg.session.of("AnswerCallbackQuery") if d.get("text")]


async def _press(tg: Harness, label: str, *, user: int = OWNER_ID) -> None:
    await tg.click(user, _inline(tg), label)


async def _say(tg: Harness, text: str, *, user: int = OWNER_ID) -> None:
    await tg.feed(text_update(user, text))


def _photo_update(user_id: int, file_id: str) -> Update:
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=datetime.now(UTC),
            chat=chat(user_id),
            from_user=tg_user(user_id),
            photo=[PhotoSize(file_id=file_id, file_unique_id="u", width=10, height=10)],
        ),
    )


async def _events(app: App) -> list[Event]:
    async with app.sessions() as session:
        return list((await session.scalars(select(Event))).all())


async def _start(tg: Harness, user: int = OWNER_ID) -> None:
    """/menu -> «Встречи» -> «Новая встреча»."""
    await tg.feed(command_update(user, "/menu"))
    mid = _inline(tg)
    await tg.click(user, mid, "Встречи")
    await tg.click(user, mid, "Новая встреча")


async def _to_time(tg: Harness) -> None:
    await _start(tg)
    await _press(tg, "Ближайшая")


async def _to_place(tg: Harness, time_label: str = "18:00") -> None:
    await _to_time(tg)
    await _press(tg, time_label)


async def _to_description(tg: Harness) -> None:
    await _to_place(tg)
    await _say(tg, "Кафе «Утро» | https://maps.example.com/xyz")


async def _to_photo(tg: Harness) -> None:
    await _to_description(tg)
    await _say(tg, "Приходите")


async def _to_capacity(tg: Harness) -> None:
    await _to_photo(tg)
    await _press(tg, "Пропустить")


async def _to_preview(tg: Harness, capacity: str = "12") -> None:
    await _to_capacity(tg)
    await _say(tg, capacity)


# ---- полный проход -------------------------------------------------------------------


async def test_полный_проход_мастера_создаёт_встречу_с_введёнными_полями(
    app: App, tg: Harness
) -> None:
    await _to_preview(tg)
    assert "Предпросмотр" in _screen(tg)
    await _press(tg, "Создать")
    events = await _events(app)
    assert len(events) == 1
    event = events[0]
    assert event.starts_at == SATURDAY_18
    assert event.capacity == 12
    assert event.place == "Кафе «Утро»"
    assert event.place_url == "https://maps.example.com/xyz"
    assert event.description_html == "Приходите"
    assert event.photo_file_id is None
    assert event.created_by == OWNER_ID


async def test_после_создания_открывается_карточка_новой_встречи(app: App, tg: Harness) -> None:
    await _to_preview(tg)
    await _press(tg, "Создать")
    text = _screen(tg)
    assert "Занято: 0 из 12" in text
    assert "Кафе «Утро»" in text


async def test_мастер_показывает_номер_шага_на_каждом_окне(tg: Harness) -> None:
    await _start(tg)
    assert "шаг 1 из 6: дата" in _screen(tg)
    await _press(tg, "Ближайшая")
    assert "шаг 2 из 6: время" in _screen(tg)
    await _press(tg, "18:00")
    assert "шаг 3 из 6: место" in _screen(tg)
    await _say(tg, "Парк")
    assert "шаг 4 из 6: текст объявления" in _screen(tg)
    await _say(tg, "Текст")
    assert "шаг 5 из 6: фото" in _screen(tg)
    await _press(tg, "Пропустить")
    assert "шаг 6 из 6: лимит мест" in _screen(tg)


async def test_предпросмотр_показывает_пост_и_кнопки_под_ним(tg: Harness) -> None:
    await _to_preview(tg)
    text = _screen(tg)
    assert "Кафе" in text
    assert "Приходите" in text
    assert "Кнопки под постом:" in text


# ---- шаг 1: дата ---------------------------------------------------------------------


async def test_кнопка_ближайшая_предлагает_ближайшую_субботу(tg: Harness) -> None:
    await _start(tg)
    assert any("Ближайшая: суббота, 24 октября" in t for t in _labels(tg))


async def test_прошедшая_дата_в_календаре_отклоняется(tg: Harness) -> None:
    await _start(tg)
    await _press(tg, "22")
    assert "Эта дата уже прошла" in _screen(tg)
    assert "шаг 1 из 6" in _screen(tg)


async def test_календарь_принимает_будущую_дату(tg: Harness) -> None:
    await _start(tg)
    await _press(tg, "28")
    assert "шаг 2 из 6: время" in _screen(tg)
    assert "28.10.2026" in _screen(tg)


# ---- шаг 2: время --------------------------------------------------------------------


async def test_время_вводится_текстом(app: App, tg: Harness) -> None:
    await _to_time(tg)
    await _say(tg, "18.30")
    assert "шаг 3 из 6: место" in _screen(tg)
    await _say(tg, "Парк")
    await _say(tg, "Текст")
    await _press(tg, "Пропустить")
    await _say(tg, "5")
    await _press(tg, "Создать")
    assert (await _events(app))[0].starts_at == datetime(2026, 10, 24, 21, 30, tzinfo=UTC)


async def test_непонятное_время_даёт_подсказку_и_шаг_остаётся(tg: Harness) -> None:
    await _to_time(tg)
    await _say(tg, "25:99")
    assert "Не удалось разобрать время" in _screen(tg)
    assert "шаг 2 из 6: время" in _screen(tg)


async def test_слишком_близкое_время_возвращает_к_выбору_даты(tg: Harness) -> None:
    await _start(tg)
    await _press(tg, "23")  # сегодня
    await _say(tg, "10:00")
    text = _screen(tg)
    assert "Моменты времени не согласованы" in text
    assert "шаг 1 из 6: дата" in text


async def test_кнопка_назад_на_времени_возвращает_к_дате(tg: Harness) -> None:
    await _to_time(tg)
    await _press(tg, "Назад")
    assert "шаг 1 из 6: дата" in _screen(tg)


# ---- шаг 3: место --------------------------------------------------------------------


async def test_место_с_некорректной_ссылкой_отклоняется(tg: Harness) -> None:
    await _to_place(tg)
    await _say(tg, "Кафе | ftp://bad")
    text = _screen(tg)
    assert "Ссылка должна начинаться с https://" in text
    assert "шаг 3 из 6: место" in text


async def test_место_без_ссылки_принимается(app: App, tg: Harness) -> None:
    await _to_place(tg)
    await _say(tg, "Просто парк")
    await _say(tg, "Текст")
    await _press(tg, "Пропустить")
    await _say(tg, "5")
    await _press(tg, "Создать")
    event = (await _events(app))[0]
    assert event.place == "Просто парк"
    assert event.place_url == ""


async def test_без_места_сохраняет_пустое_место(app: App, tg: Harness) -> None:
    await _to_place(tg)
    await _press(tg, "Без места")
    await _press(tg, "Без текста")
    await _press(tg, "Пропустить")
    await _say(tg, "5")
    await _press(tg, "Создать")
    event = (await _events(app))[0]
    assert event.place == ""
    assert event.description_html == ""


async def test_место_по_умолчанию_берётся_из_настроек(app: App, tg: Harness) -> None:
    await app.services.settings.set("default_place", "Библиотека", OWNER_ID)
    await app.services.settings.set("default_place_url", "https://maps.example.com/lib", OWNER_ID)
    await _to_place(tg)
    assert "Место по умолчанию: Библиотека" in _screen(tg)
    await _press(tg, "По умолчанию")
    await _press(tg, "Без текста")
    await _press(tg, "Пропустить")
    await _say(tg, "5")
    await _press(tg, "Создать")
    event = (await _events(app))[0]
    assert event.place == "Библиотека"
    assert event.place_url == "https://maps.example.com/lib"


# ---- шаг 4: текст --------------------------------------------------------------------


async def test_кнопки_по_умолчанию_для_текста_нет_пока_он_не_задан(tg: Harness) -> None:
    await _to_description(tg)
    assert not any("По умолчанию" in t for t in _labels(tg))


async def test_текст_по_умолчанию_берётся_из_настроек(app: App, tg: Harness) -> None:
    await app.services.settings.set("default_description", "Стандартный текст", OWNER_ID)
    await _to_description(tg)
    await _press(tg, "По умолчанию")
    await _press(tg, "Пропустить")
    await _say(tg, "5")
    await _press(tg, "Создать")
    assert (await _events(app))[0].description_html == "Стандартный текст"


# ---- шаг 5: фото ---------------------------------------------------------------------


async def test_не_фото_на_шаге_фото_даёт_подсказку(tg: Harness) -> None:
    await _to_photo(tg)
    await _say(tg, "это текст")
    assert "Это не фото" in _screen(tg)
    assert "шаг 5 из 6" in _screen(tg)


async def test_присланное_фото_сохраняется_во_встрече(app: App, tg: Harness) -> None:
    await _to_photo(tg)
    await tg.feed(_photo_update(OWNER_ID, "photo-id-1"))
    assert "шаг 6 из 6: лимит мест" in _screen(tg)
    await _say(tg, "7")
    await _press(tg, "Создать")
    assert (await _events(app))[0].photo_file_id == "photo-id-1"


async def test_фото_по_умолчанию_берётся_из_настроек(app: App, tg: Harness) -> None:
    await app.services.settings.set("default_photo_file_id", "default-photo", OWNER_ID)
    await _to_photo(tg)
    await _press(tg, "По умолчанию")
    await _say(tg, "7")
    await _press(tg, "Создать")
    assert (await _events(app))[0].photo_file_id == "default-photo"


# ---- шаг 6: лимит --------------------------------------------------------------------


async def test_лимит_не_число_отклоняется(tg: Harness) -> None:
    await _to_capacity(tg)
    await _say(tg, "много")
    assert "Нужно целое число от 1 до 200" in _screen(tg)
    assert "шаг 6 из 6" in _screen(tg)


async def test_лимит_вне_диапазона_отклоняется(tg: Harness) -> None:
    await _to_capacity(tg)
    await _say(tg, "0")
    assert "Лимит мест должен быть числом от 1 до 200." in _screen(tg)
    assert "шаг 6 из 6" in _screen(tg)


async def test_лимит_по_умолчанию_из_настроек(app: App, tg: Harness) -> None:
    await app.services.settings.set("capacity", 33, OWNER_ID)
    await _to_capacity(tg)
    await _press(tg, "По умолчанию (33)")
    await _press(tg, "Создать")
    assert (await _events(app))[0].capacity == 33


# ---- назад, отмена, права --------------------------------------------------------------


async def test_назад_проходит_шаги_в_обратном_порядке(tg: Harness) -> None:
    await _to_preview(tg)
    await _press(tg, "Назад")
    assert "шаг 6 из 6" in _screen(tg)
    await _press(tg, "Назад")
    assert "шаг 5 из 6" in _screen(tg)
    await _press(tg, "Назад")
    assert "шаг 4 из 6" in _screen(tg)
    await _press(tg, "Назад")
    assert "шаг 3 из 6" in _screen(tg)


async def test_отмена_закрывает_мастер_и_не_создаёт_встречу(app: App, tg: Harness) -> None:
    await _to_preview(tg)
    await _press(tg, "Отмена")
    assert await _events(app) == []
    assert "Встречи" in _screen(tg)
    assert "Новая встреча" not in _screen(tg)


async def test_создание_после_того_как_время_прошло_возвращает_к_дате(
    app: App, tg: Harness
) -> None:
    await _to_preview(tg)
    app.clock.advance(timedelta(days=3))
    await _press(tg, "Создать")
    text = _screen(tg)
    assert "Выберите дату заново" in text
    assert "шаг 1 из 6: дата" in text
    assert await _events(app) == []


async def test_не_админ_не_запускает_мастер(tg: Harness) -> None:
    await tg.feed(command_update(777, "/menu"))
    assert tg.session.of("SendMessage")[-1]["text"] == "Нет доступа."
    assert not tg.session.of("EditMessageText")


async def test_админ_потерявший_права_не_может_продолжить_мастер(app: App, tg: Harness) -> None:
    await _start(tg, ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await _press(tg, "Ближайшая", user=ADMIN_ID)
    assert _alerts(tg)[-1] == "Нет доступа."
    assert await _events(app) == []


async def test_админ_потерявший_права_не_может_создать_встречу(app: App, tg: Harness) -> None:
    await _start(tg, ADMIN_ID)
    await _press(tg, "Ближайшая", user=ADMIN_ID)
    await _press(tg, "18:00", user=ADMIN_ID)
    await tg.feed(text_update(ADMIN_ID, "Парк"))
    await tg.feed(text_update(ADMIN_ID, "Текст"))
    await _press(tg, "Пропустить", user=ADMIN_ID)
    await tg.feed(text_update(ADMIN_ID, "5"))
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await _press(tg, "Создать", user=ADMIN_ID)
    assert _alerts(tg)[-1] == "Нет доступа."
    assert await _events(app) == []


import pytest as _pytest  # noqa: E402


@_pytest.fixture(autouse=True)
def _свежие_роутеры() -> None:
    """Роутеры модулей создаются при импорте один раз, а Harness собирает корень заново.

    Без отсоединения второй тест падает с «Router is already attached».
    """
    import importlib

    from meetup_bot.dialogs import MODULES
    from meetup_bot.handlers import attendance, errors, inline, signup, start

    routers = [errors.router, signup.router, attendance.router, inline.router, start.router]
    for name in MODULES:
        module = importlib.import_module(name)
        if getattr(module, "router", None) is not None:
            routers.append(module.router)
        routers.extend(getattr(module, "DIALOGS", []))
    for router in routers:
        router._parent_router = None
