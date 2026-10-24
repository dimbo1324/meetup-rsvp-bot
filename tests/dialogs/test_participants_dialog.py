"""Диалог участников встречи: список, добавление, удаление, неявка и бан."""

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.types import Message, SharedUser, Update, UsersShared
from sqlalchemy import select

from meetup_bot.db.models import Attendance, Registration, RegistrationStatus, User
from tests.helpers import ADMIN_ID, OWNER_ID, App, person
from tests.tg_fake import Harness, chat, command_update, next_id, text_update, tg_user

# ---- локальные помощники -------------------------------------------------------------


def _inline(tg: Harness) -> int:
    ids = [m for m, mk in tg.session.last_markup.items() if "inline_keyboard" in mk]
    return max(ids)


def _labels(tg: Harness) -> list[str]:
    return [text for text, _ in tg.session.buttons(_inline(tg))]


def _screen(tg: Harness) -> str:
    for name, data in reversed(tg.session.calls):
        if name in {"SendMessage", "EditMessageText"} and "text" in data:
            return str(data["text"])
    return ""


def _said(tg: Harness, fragment: str) -> bool:
    return any(fragment in str(data.get("text", "")) for _, data in tg.session.calls)


def _alerts(tg: Harness) -> list[str]:
    return [str(d["text"]) for d in tg.session.of("AnswerCallbackQuery") if d.get("text")]


async def _press(tg: Harness, label: str, *, user: int = OWNER_ID) -> None:
    await tg.click(user, _inline(tg), label)


async def _say(tg: Harness, text: str, *, user: int = OWNER_ID) -> None:
    await tg.feed(text_update(user, text))


def _shared_update(sender: int, target: int, first_name: str | None = "Борис") -> Update:
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=datetime.now(UTC),
            chat=chat(sender),
            from_user=tg_user(sender),
            users_shared=UsersShared(
                request_id=1, users=[SharedUser(user_id=target, first_name=first_name)]
            ),
        ),
    )


async def _registrations(app: App, event_id: int) -> list[Registration]:
    async with app.sessions() as session:
        rows = await session.scalars(
            select(Registration).where(Registration.event_id == event_id).order_by(Registration.id)
        )
        return list(rows.all())


async def _banned_ids(app: App) -> set[int]:
    return {ban.user_id for ban in await app.services.bans.list_banned(OWNER_ID)}


async def _make_event(app: App, capacity: int = 3) -> int:
    created = await app.services.events.create_event_as_admin(
        OWNER_ID, capacity=capacity, place="Кафе"
    )
    return created.event_id


@pytest.fixture
async def event_id(app: App) -> int:
    return await _make_event(app)


async def _open(tg: Harness, user: int = OWNER_ID) -> None:
    """/menu -> «Встречи» -> встреча -> «Участники»."""
    await tg.feed(command_update(user, "/menu"))
    mid = _inline(tg)
    await tg.click(user, mid, "Встречи")
    await tg.click(user, mid, "занято")
    await tg.click(user, mid, "Участники")


async def _register(app: App, event_id: int, *numbers: int) -> None:
    for n in numbers:
        await app.services.registration.register(event_id, person(n))


def _after_deadline(app: App) -> None:
    # Дедлайн отмены — пятница 20:00 по Монтевидео (23:00 UTC); часы стоят на 12:00 UTC
    app.clock.advance(timedelta(hours=12))


# ---- список --------------------------------------------------------------------------


async def test_пустой_список_участников(tg: Harness, event_id: int) -> None:
    await _open(tg)
    text = _screen(tg)
    assert "Пока никого нет." in text
    assert "Занято мест: 0 из 3" in text


async def test_список_показывает_активных_и_очередь(app: App, tg: Harness, event_id: int) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    await _register(app, event_id, 1, 2, 3, 4)
    await _open(tg)
    labels = _labels(tg)
    assert "1. Участник1" in labels
    assert "3. Участник3" in labels
    assert "⏳ 1. Участник4" in labels
    text = _screen(tg)
    assert "Занято мест: 3 из 3" in text
    assert "В очереди: 1" in text


async def test_гость_помечен_в_списке(app: App, tg: Harness, event_id: int) -> None:
    await app.services.settings.set("plus_one_enabled", True, OWNER_ID)
    await app.services.registration.register(event_id, person(1), guests=1)
    await _open(tg)
    assert "1. Участник1 (+1)" in _labels(tg)


async def test_карточка_участника_показывает_источник_записи(
    app: App, tg: Harness, event_id: int
) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "1. Участник1")
    text = _screen(tg)
    assert "Участник1" in text
    assert "@user1" in text
    assert "Статус: записан(а)" in text
    assert "сам записался" in text


async def test_карточка_участника_из_очереди(app: App, tg: Harness) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await _make_event(app, capacity=1)
    await _register(app, event_id, 1, 2)
    await _open(tg)
    await _press(tg, "⏳ 1. Участник2")
    assert "Статус: в очереди, место 1" in _screen(tg)


async def test_кнопка_к_встрече_закрывает_диалог_участников(tg: Harness, event_id: int) -> None:
    await _open(tg)
    await _press(tg, "К встрече")
    assert "Статус: запись открыта" in _screen(tg)


# ---- добавление вручную ------------------------------------------------------------------


async def test_добавление_по_имени_вручную(app: App, tg: Harness, event_id: int) -> None:
    await _open(tg)
    await _press(tg, "Добавить участника")
    await _press(tg, "Ввести имя вручную")
    await _say(tg, "Анна Петрова")
    rows = await _registrations(app, event_id)
    assert len(rows) == 1
    assert rows[0].manual_name == "Анна Петрова"
    assert rows[0].user_id is None
    assert rows[0].source == "admin"
    assert "✅ Добавлен(а): Анна Петрова." in _screen(tg)
    assert "1. Анна Петрова (вручную)" in _labels(tg)


async def test_пустое_и_слишком_длинное_имя_отклоняются(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open(tg)
    await _press(tg, "Добавить участника")
    await _press(tg, "Ввести имя вручную")
    await _say(tg, "Я" * 101)
    assert "Слишком длинно: 101 из 100 символов." in _screen(tg)
    assert await _registrations(app, event_id) == []
    # Окно ввода осталось: корректное имя принимается
    await _say(tg, "Борис")
    assert len(await _registrations(app, event_id)) == 1


async def test_назад_из_ввода_имени_и_из_выбора_способа(tg: Harness, event_id: int) -> None:
    await _open(tg)
    await _press(tg, "Добавить участника")
    await _press(tg, "Ввести имя вручную")
    await _press(tg, "Назад")
    assert "Кого записать?" in _screen(tg)
    await _press(tg, "К списку")
    assert "Участники" in _screen(tg)
    assert "Кого записать?" not in _screen(tg)


async def test_сверх_лимита_требует_подтверждения(app: App, tg: Harness) -> None:
    event_id = await _make_event(app, capacity=1)
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "Добавить участника")
    await _press(tg, "Ввести имя вручную")
    await _say(tg, "Анна")
    assert "Мест не хватает." in _screen(tg)
    assert len(await _registrations(app, event_id)) == 1
    await _press(tg, "Добавить сверх лимита")
    rows = await _registrations(app, event_id)
    assert len(rows) == 2
    assert rows[1].manual_name == "Анна"
    assert "✅ Добавлен(а): Анна." in _screen(tg)
    assert "Занято мест: 2 из 1" in _screen(tg)


async def test_отказ_от_добавления_сверх_лимита(app: App, tg: Harness) -> None:
    event_id = await _make_event(app, capacity=1)
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "Добавить участника")
    await _press(tg, "Ввести имя вручную")
    await _say(tg, "Анна")
    await _press(tg, "К списку")
    assert len(await _registrations(app, event_id)) == 1
    assert "Занято мест: 1 из 1" in _screen(tg)


# ---- добавление через выбор пользователя -------------------------------------------------


async def test_добавление_через_выбор_пользователя_telegram(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open(tg)
    await _press(tg, "Добавить участника")
    await _press(tg, "Выбрать пользователя Telegram")
    await tg.feed(_shared_update(OWNER_ID, 555, "Борис"))
    rows = await _registrations(app, event_id)
    assert len(rows) == 1
    assert rows[0].user_id == 555
    assert rows[0].source == "admin"
    assert "✅ Добавлен(а): Борис." in _screen(tg)


async def test_выбранный_пользователь_уже_записан_отклоняется_пикером(
    app: App, tg: Harness, event_id: int
) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "Добавить участника")
    await _press(tg, "Выбрать пользователя Telegram")
    await tg.feed(_shared_update(OWNER_ID, person(1).id, "Участник1"))
    assert _said(tg, "Этого пользователя выбрать нельзя.")
    assert len(await _registrations(app, event_id)) == 1


async def test_выбор_пользователя_сверх_лимита_идёт_через_подтверждение(
    app: App, tg: Harness
) -> None:
    event_id = await _make_event(app, capacity=1)
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "Добавить участника")
    await _press(tg, "Выбрать пользователя Telegram")
    await tg.feed(_shared_update(OWNER_ID, 555, "Борис"))
    assert "Мест не хватает." in _screen(tg)
    await _press(tg, "Добавить сверх лимита")
    assert {r.user_id for r in await _registrations(app, event_id)} == {person(1).id, 555}


# ---- удаление ----------------------------------------------------------------------------


async def test_удаление_участника_с_подтверждением(app: App, tg: Harness, event_id: int) -> None:
    await _register(app, event_id, 1, 2)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Удалить из встречи")
    assert "Удалить участника из встречи?" in _screen(tg)
    assert "Участник1" in _screen(tg)
    await _press(tg, "Да")
    rows = await _registrations(app, event_id)
    assert rows[0].status == RegistrationStatus.REMOVED.value
    assert rows[1].status == RegistrationStatus.ACTIVE.value
    assert "✅ Участник удалён." in _screen(tg)
    assert "Занято мест: 1 из 3" in _screen(tg)


async def test_отказ_от_удаления_ничего_не_меняет(app: App, tg: Harness, event_id: int) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Удалить из встречи")
    await _press(tg, "Нет")
    assert (await _registrations(app, event_id))[0].status == RegistrationStatus.ACTIVE.value
    assert "Статус: записан(а)" in _screen(tg)


async def test_удаление_переводит_человека_из_очереди(app: App, tg: Harness) -> None:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = await _make_event(app, capacity=2)
    await _register(app, event_id, 1, 2, 3)
    async with app.sessions() as session, session.begin():
        waiting = await session.get(User, person(3).id)
        assert waiting is not None
        waiting.dm_available = True  # личные сообщения уходят только тем, кто нажимал /start
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Удалить из встречи")
    await _press(tg, "Да")
    assert "Из очереди переведено: 1." in _screen(tg)
    rows = await _registrations(app, event_id)
    assert rows[2].status == RegistrationStatus.ACTIVE.value
    assert app.notifier.dms_to(person(3).id)


async def test_удаление_записи_добавленной_вручную(app: App, tg: Harness, event_id: int) -> None:
    await app.services.participants.admin_add(OWNER_ID, event_id, manual_name="Анна")
    await _open(tg)
    await _press(tg, "1. Анна (вручную)")
    assert "Забанить" not in " | ".join(_labels(tg))
    await _press(tg, "Удалить из встречи")
    await _press(tg, "Да")
    assert (await _registrations(app, event_id))[0].status == RegistrationStatus.REMOVED.value


async def test_повторное_удаление_уже_удалённой_записи_показывает_предупреждение(
    app: App, tg: Harness, event_id: int
) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Удалить из встречи")
    registration_id = (await _registrations(app, event_id))[0].id
    # Пока диалог ждёт подтверждения, запись удалил другой админ
    await app.services.participants.admin_remove(OWNER_ID, event_id, registration_id)
    await _press(tg, "Да")
    assert "⚠️" in _screen(tg)
    assert (await _registrations(app, event_id))[0].status == RegistrationStatus.REMOVED.value


# ---- неявка после дедлайна --------------------------------------------------------------------


async def test_до_дедлайна_кнопок_неявки_нет(app: App, tg: Harness, event_id: int) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "1. Участник1")
    labels = " | ".join(_labels(tg))
    assert "Удалить из встречи" in labels
    assert "Отменить как неявку" not in labels
    assert "Отменить без штрафа" not in labels


async def test_отмена_как_неявка_после_дедлайна_банит_по_порогу(
    app: App, tg: Harness, event_id: int
) -> None:
    await _register(app, event_id, 1)
    _after_deadline(app)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Отменить как неявку")
    assert "Отменить запись как неявку?" in _screen(tg)
    await _press(tg, "Да")
    registration = (await _registrations(app, event_id))[0]
    assert registration.status == RegistrationStatus.CANCELLED.value
    assert registration.attendance == Attendance.NO_SHOW.value
    assert "✅ Запись отменена как неявка." in _screen(tg)
    assert "За неявки участник автоматически забанен." in _screen(tg)
    assert person(1).id in await _banned_ids(app)


async def test_отмена_без_штрафа_после_дедлайна_не_банит(
    app: App, tg: Harness, event_id: int
) -> None:
    await _register(app, event_id, 1)
    _after_deadline(app)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Отменить без штрафа")
    assert "Отменить запись без штрафа?" in _screen(tg)
    await _press(tg, "Да")
    # Без штрафа запись снимается как удалённая, а не как неявка
    registration = (await _registrations(app, event_id))[0]
    assert registration.status == RegistrationStatus.REMOVED.value
    assert registration.attendance != Attendance.NO_SHOW.value
    assert await _banned_ids(app) == set()


async def test_неявка_при_выключенном_автобане_не_банит(
    app: App, tg: Harness, event_id: int
) -> None:
    await app.services.settings.set("ban_after_no_shows", 0, OWNER_ID)
    await _register(app, event_id, 1)
    _after_deadline(app)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Отменить как неявку")
    await _press(tg, "Да")
    assert "✅ Запись отменена как неявка." in _screen(tg)
    assert "автоматически забанен" not in _screen(tg)
    assert await _banned_ids(app) == set()


# ---- ручной бан --------------------------------------------------------------------------


async def test_бан_участника_с_причиной(app: App, tg: Harness, event_id: int) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Забанить")
    assert "Забанить: Участник1?" in _screen(tg)
    await _say(tg, "Спам в чате")
    assert "🚫 Участник1: забанен(а)." in _screen(tg)
    bans = await app.services.bans.list_banned(OWNER_ID)
    assert [(b.user_id, b.comment, b.banned_by) for b in bans] == [
        (person(1).id, "Спам в чате", OWNER_ID)
    ]


async def test_бан_участника_без_причины(app: App, tg: Harness, event_id: int) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Забанить")
    await _press(tg, "Без причины")
    bans = await app.services.bans.list_banned(OWNER_ID)
    assert [(b.user_id, b.comment) for b in bans] == [(person(1).id, None)]


async def test_слишком_длинная_причина_бана_отклоняется(
    app: App, tg: Harness, event_id: int
) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Забанить")
    await _say(tg, "П" * 201)
    assert "Слишком длинно: 201 из 200 символов." in _screen(tg)
    assert await _banned_ids(app) == set()
    await _say(tg, "Коротко")
    assert person(1).id in await _banned_ids(app)


async def test_отмена_бана_кнопкой_назад(app: App, tg: Harness, event_id: int) -> None:
    await _register(app, event_id, 1)
    await _open(tg)
    await _press(tg, "1. Участник1")
    await _press(tg, "Забанить")
    await _press(tg, "Назад")
    assert "Статус: записан(а)" in _screen(tg)
    assert await _banned_ids(app) == set()


# ---- права -------------------------------------------------------------------------------


async def test_админ_потерявший_права_не_добавляет_участников(
    app: App, tg: Harness, event_id: int
) -> None:
    await _open(tg, ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.click(ADMIN_ID, _inline(tg), "Добавить участника")
    assert _alerts(tg)[-1] == "Нет доступа."
    assert await _registrations(app, event_id) == []


async def test_админ_потерявший_права_не_удаляет_и_не_банит(
    app: App, tg: Harness, event_id: int
) -> None:
    await _register(app, event_id, 1)
    await _open(tg, ADMIN_ID)
    await tg.click(ADMIN_ID, _inline(tg), "1. Участник1")
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.click(ADMIN_ID, _inline(tg), "Забанить")
    assert _alerts(tg)[-1] == "Нет доступа."
    assert await _banned_ids(app) == set()
    assert (await _registrations(app, event_id))[0].status == RegistrationStatus.ACTIVE.value


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
