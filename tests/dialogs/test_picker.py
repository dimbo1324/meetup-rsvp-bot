"""Выбор пользователя Telegram: системный выбор, пересланное сообщение, список знакомых.

Пикер запускается из реальных диалогов: из чёрного списка (результат виден по окну причины
бана) и из участников встречи (там действует список исключений).
"""

from datetime import UTC, datetime

from aiogram.types import (
    Message,
    MessageOriginHiddenUser,
    MessageOriginUser,
    SharedUser,
    Update,
    UsersShared,
)

from tests.helpers import ADMIN_ID, OWNER_ID, App, person
from tests.tg_fake import Harness, chat, command_update, next_id, text_update, tg_user


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


def _message(user_id: int, **fields: object) -> Update:
    return Update(
        update_id=next_id(),
        message=Message(
            message_id=next_id(),
            date=datetime.now(UTC),
            chat=chat(user_id),
            from_user=tg_user(user_id),
            **fields,  # type: ignore[arg-type]
        ),
    )


def _shared(sender: int, target: int, **user_fields: object) -> Update:
    return _message(
        sender,
        users_shared=UsersShared(
            request_id=1,
            users=[SharedUser(user_id=target, **user_fields)],  # type: ignore[arg-type]
        ),
    )


def _forward(sender: int, origin_user_id: int, name: str, *, is_bot: bool = False) -> Update:
    return _message(
        sender,
        text="пересланное",
        forward_origin=MessageOriginUser(
            date=datetime.now(UTC),
            sender_user=tg_user(origin_user_id, name).model_copy(update={"is_bot": is_bot}),
        ),
    )


def _reply_keyboard(tg: Harness) -> list[str]:
    for name, data in reversed(tg.session.calls):
        markup = data.get("reply_markup")
        if name == "SendMessage" and isinstance(markup, dict) and "keyboard" in markup:
            return [button["text"] for row in markup["keyboard"] for button in row]
    return []


async def _reply_press(tg: Harness, fragment: str, user: int = OWNER_ID) -> None:
    """Нажатие кнопки reply-клавиатуры = отправка её текста."""
    text = next(t for t in _reply_keyboard(tg) if fragment in t)
    await tg.feed(text_update(user, text))


async def _open_picker(tg: Harness, user: int = OWNER_ID) -> None:
    """/menu -> «Чёрный список» -> «Забанить»: открывается выбор пользователя."""
    await tg.feed(command_update(user, "/menu"))
    mid = _inline(tg)
    await tg.click(user, mid, "Чёрный список")
    await tg.click(user, mid, "Забанить")


async def _make_known(app: App, count: int) -> int:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=50)).event_id
    for n in range(1, count + 1):
        await app.services.registration.register(event_id, person(n))
    return event_id


# ---- системный выбор -------------------------------------------------------------------


async def test_пикер_показывает_заголовок_и_reply_кнопки(tg: Harness) -> None:
    await _open_picker(tg)
    assert "Кого забанить?" in _screen(tg)
    keyboard = _reply_keyboard(tg)
    assert any("Выбрать пользователя" in t for t in keyboard)
    assert any("Из списка записывавшихся" in t for t in keyboard)
    assert any("Отмена" in t for t in keyboard)


async def test_выбор_пользователя_кнопкой_telegram_возвращает_результат(tg: Harness) -> None:
    await _open_picker(tg)
    await tg.feed(_shared(OWNER_ID, 555, first_name="Борис", username="boris"))
    assert "Бан: <b>Борис (@boris)</b>" in _screen(tg)


async def test_после_выбора_reply_клавиатура_снимается(tg: Harness) -> None:
    await _open_picker(tg)
    await tg.feed(_shared(OWNER_ID, 555, first_name="Борис"))
    removals = [
        d
        for d in tg.session.of("SendMessage")
        if isinstance(d.get("reply_markup"), dict) and d["reply_markup"].get("remove_keyboard")
    ]
    assert removals


async def test_без_имени_подставляется_username(tg: Harness) -> None:
    await _open_picker(tg)
    await tg.feed(_shared(OWNER_ID, 555, username="boris"))
    assert "@boris" in _screen(tg)


async def test_без_имени_и_username_подставляется_заглушка(tg: Harness) -> None:
    await _open_picker(tg)
    await tg.feed(_shared(OWNER_ID, 555))
    assert "Без имени" in _screen(tg)


# ---- пересланное сообщение --------------------------------------------------------------


async def test_пересланное_сообщение_определяет_пользователя(tg: Harness) -> None:
    await _open_picker(tg)
    await tg.feed(_forward(OWNER_ID, 777, "Вера"))
    assert "Бан: <b>Вера (@u777)</b>" in _screen(tg)


async def test_пересланное_от_бота_отклоняется(tg: Harness) -> None:
    await _open_picker(tg)
    await tg.feed(_forward(OWNER_ID, 888, "Бот", is_bot=True))
    assert _said(tg, "Ботов выбирать нельзя.")
    assert _screen(tg) == "Ботов выбирать нельзя."


async def test_скрытый_профиль_отклоняется_с_объяснением(tg: Harness) -> None:
    await _open_picker(tg)
    await tg.feed(
        _message(
            OWNER_ID,
            text="привет",
            forward_origin=MessageOriginHiddenUser(
                date=datetime.now(UTC), sender_user_name="Скрытный"
            ),
        )
    )
    assert _said(tg, "Профиль этого человека скрыт настройками приватности")
    assert _screen(tg).startswith("Профиль этого человека скрыт")


async def test_посторонний_текст_даёт_подсказку(tg: Harness) -> None:
    await _open_picker(tg)
    await tg.feed(text_update(OWNER_ID, "просто текст"))
    assert _said(tg, "перешлите сюда любое сообщение этого человека")


# ---- отмена -------------------------------------------------------------------------------


async def test_отмена_возвращает_к_предыдущему_окну(tg: Harness) -> None:
    await _open_picker(tg)
    await _reply_press(tg, "Отмена")
    assert "Банов нет." in _screen(tg)


# ---- список известных --------------------------------------------------------------------


async def test_список_известных_показывает_людей_и_выбор_возвращает_результат(
    app: App, tg: Harness
) -> None:
    await _make_known(app, 2)
    await _open_picker(tg)
    await _reply_press(tg, "Из списка записывавшихся")
    assert "Известные пользователи, страница 1 из 1." in _screen(tg)
    labels = _labels(tg)
    assert "Участник1 (@user1)" in labels
    assert "Участник2 (@user2)" in labels
    await tg.click(OWNER_ID, _inline(tg), "Участник2")
    assert "Бан: <b>Участник2 (@user2)</b>" in _screen(tg)


async def test_список_известных_разбит_на_страницы(app: App, tg: Harness) -> None:
    # 10 записавшихся и админ с именем = 11 известных, по 8 на страницу
    await _make_known(app, 10)
    await _open_picker(tg)
    await _reply_press(tg, "Из списка записывавшихся")
    assert "страница 1 из 2" in _screen(tg)
    assert not any(t == "◀" for t in _labels(tg))
    await tg.click(OWNER_ID, _inline(tg), "▶")
    assert "страница 2 из 2" in _screen(tg)
    assert not any(t == "▶" for t in _labels(tg))
    await tg.click(OWNER_ID, _inline(tg), "◀")
    assert "страница 1 из 2" in _screen(tg)


async def test_список_известных_не_показывает_заглушки_без_имени(app: App, tg: Harness) -> None:
    await app.services.bans.ban(OWNER_ID, 5555)  # запись-заглушка без имени
    await _open_picker(tg)
    await _reply_press(tg, "Из списка записывавшихся")
    assert not any("5555" in t for t in _labels(tg))


async def test_назад_из_списка_известных_возвращает_к_выбору(app: App, tg: Harness) -> None:
    await _make_known(app, 1)
    await _open_picker(tg)
    await _reply_press(tg, "Из списка записывавшихся")
    await tg.click(OWNER_ID, _inline(tg), "Назад")
    assert "Кого забанить?" in _screen(tg)


async def test_отмена_в_списке_известных_закрывает_пикер(app: App, tg: Harness) -> None:
    await _make_known(app, 1)
    await _open_picker(tg)
    await _reply_press(tg, "Из списка записывавшихся")
    await tg.click(OWNER_ID, _inline(tg), "Отмена")
    assert "Банов нет." in _screen(tg)


# ---- исключения ---------------------------------------------------------------------------


async def _open_participants_picker(app: App, tg: Harness) -> int:
    """Встреча с одним записанным и одним удалённым; открыт пикер добавления участника."""
    event_id = await _make_known(app, 2)
    registrations = await app.services.participants.list_participants(OWNER_ID, event_id)
    removed = next(r for r in registrations.active if r.user_id == person(2).id)
    await app.services.participants.admin_remove(OWNER_ID, event_id, removed.registration_id)
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    await tg.click(OWNER_ID, mid, "занято")
    await tg.click(OWNER_ID, mid, "Участники")
    await tg.click(OWNER_ID, mid, "Добавить участника")
    await tg.click(OWNER_ID, mid, "Выбрать пользователя Telegram")
    return event_id


async def test_известные_не_включают_уже_записанных(app: App, tg: Harness) -> None:
    await _open_participants_picker(app, tg)
    await _reply_press(tg, "Из списка записывавшихся")
    labels = _labels(tg)
    assert "Участник2 (@user2)" in labels
    assert not any(t.startswith("Участник1") for t in labels)


async def test_системный_выбор_уже_записанного_отклоняется(app: App, tg: Harness) -> None:
    await _open_participants_picker(app, tg)
    await tg.feed(_shared(OWNER_ID, person(1).id, first_name="Участник1"))
    assert _said(tg, "Этого пользователя выбрать нельзя.")


async def test_пересланное_от_уже_записанного_отклоняется(app: App, tg: Harness) -> None:
    await _open_participants_picker(app, tg)
    await tg.feed(_forward(OWNER_ID, person(1).id, "Участник1"))
    assert _said(tg, "Этого пользователя выбрать нельзя.")


async def test_исключённый_не_блокирует_выбор_остальных(app: App, tg: Harness) -> None:
    event_id = await _open_participants_picker(app, tg)
    await tg.feed(_shared(OWNER_ID, 555, first_name="Борис"))
    assert "✅ Добавлен(а): Борис." in _screen(tg)
    assert event_id


# ---- права -------------------------------------------------------------------------------


async def test_потерявший_права_админ_не_может_выбрать_пользователя(app: App, tg: Harness) -> None:
    await _open_picker(tg, ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.feed(_shared(ADMIN_ID, 555, first_name="Борис"))
    assert _said(tg, "Нет доступа.")
    assert "Бан: Борис" not in _screen(tg)


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
