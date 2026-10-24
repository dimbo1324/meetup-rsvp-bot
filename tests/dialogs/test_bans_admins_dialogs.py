"""Диалоги чёрного списка и управления админами."""

from datetime import UTC, datetime

from aiogram.types import Message, SharedUser, Update, UsersShared
from sqlalchemy import select

from meetup_bot.db.models import Admin, Ban, BanReason
from tests.helpers import ADMIN_ID, OWNER_ID, App, person
from tests.tg_fake import (
    Harness,
    callback_update,
    chat,
    command_update,
    next_id,
    text_update,
    tg_user,
)

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


async def _open_section(tg: Harness, label: str, user: int = OWNER_ID) -> int:
    await tg.feed(command_update(user, "/menu"))
    mid = _inline(tg)
    await tg.click(user, mid, label)
    return mid


async def _bans(app: App) -> list[Ban]:
    async with app.sessions() as session:
        return list((await session.scalars(select(Ban).order_by(Ban.id))).all())


async def _admin_ids(app: App) -> set[int]:
    async with app.sessions() as session:
        return set((await session.scalars(select(Admin.user_id))).all())


async def _ban_registered(app: App, number: int = 1, comment: str | None = None) -> int:
    """Забанить человека, который уже писал боту (его имя известно)."""
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=5)).event_id
    await app.services.registration.register(event_id, person(number))
    await app.services.bans.ban(OWNER_ID, person(number).id, comment=comment)
    return person(number).id


# ---- чёрный список ---------------------------------------------------------------------


async def test_пустой_чёрный_список(tg: Harness) -> None:
    await _open_section(tg, "Чёрный список")
    assert "Банов нет." in _screen(tg)
    assert any("Забанить" in t for t in _labels(tg))


async def test_список_банов_показывает_имя_причину_и_дату(app: App, tg: Harness) -> None:
    await _ban_registered(app, 1)
    await _open_section(tg, "Чёрный список")
    assert "Сейчас в бане: 1." in _screen(tg)
    assert "Участник1 (@user1) · вручную · 23.10.2026" in _labels(tg)


async def test_бан_неизвестного_человека_показан_по_id(app: App, tg: Harness) -> None:
    await app.services.bans.ban(OWNER_ID, 5555)
    await _open_section(tg, "Чёрный список")
    assert any(t.startswith("Пользователь 5555 · вручную") for t in _labels(tg))


async def test_ручной_бан_через_выбор_пользователя_с_причиной(app: App, tg: Harness) -> None:
    mid = await _open_section(tg, "Чёрный список")
    await tg.click(OWNER_ID, mid, "Забанить")
    await tg.feed(_shared_update(OWNER_ID, 555, "Борис"))
    assert "Бан: <b>Борис</b>" in _screen(tg)
    await _say(tg, "Грубил в чате")
    bans = await _bans(app)
    assert [(b.user_id, b.reason, b.comment, b.active) for b in bans] == [
        (555, BanReason.MANUAL.value, "Грубил в чате", True)
    ]
    assert _said(tg, "Пользователь забанен.")
    text = _screen(tg)
    assert "Борис" in text
    assert "Комментарий: Грубил в чате" in text


async def test_ручной_бан_без_причины(app: App, tg: Harness) -> None:
    mid = await _open_section(tg, "Чёрный список")
    await tg.click(OWNER_ID, mid, "Забанить")
    await tg.feed(_shared_update(OWNER_ID, 555, "Борис"))
    await _press(tg, "Без причины")
    assert _alerts(tg)[-1] == "Пользователь забанен."
    bans = await _bans(app)
    assert [(b.user_id, b.comment) for b in bans] == [(555, None)]


async def test_выбранному_через_пикер_сохраняется_имя(app: App, tg: Harness) -> None:
    mid = await _open_section(tg, "Чёрный список")
    await tg.click(OWNER_ID, mid, "Забанить")
    await tg.feed(_shared_update(OWNER_ID, 555, "Борис"))
    await _press(tg, "Без причины")
    assert "Борис" in _screen(tg)


async def test_админа_забанить_нельзя(app: App, tg: Harness) -> None:
    mid = await _open_section(tg, "Чёрный список")
    await tg.click(OWNER_ID, mid, "Забанить")
    await tg.feed(_shared_update(OWNER_ID, ADMIN_ID, "Админ"))
    await _press(tg, "Без причины")
    assert _alerts(tg)[-1] == "Нельзя банить админа, владельца или самого себя."
    assert await _bans(app) == []
    assert "Банов нет." in _screen(tg)


async def test_слишком_длинная_причина_бана_отклоняется_и_окно_остаётся(
    app: App, tg: Harness
) -> None:
    mid = await _open_section(tg, "Чёрный список")
    await tg.click(OWNER_ID, mid, "Забанить")
    await tg.feed(_shared_update(OWNER_ID, 555, "Борис"))
    await _say(tg, "П" * 201)
    assert _said(tg, "Причина должна быть непустой и не длиннее 200 символов.")
    assert await _bans(app) == []
    await _say(tg, "Нормально")
    assert len(await _bans(app)) == 1


async def test_отмена_на_шаге_причины_возвращает_к_списку(app: App, tg: Harness) -> None:
    mid = await _open_section(tg, "Чёрный список")
    await tg.click(OWNER_ID, mid, "Забанить")
    await tg.feed(_shared_update(OWNER_ID, 555, "Борис"))
    await _press(tg, "Отмена")
    assert "Банов нет." in _screen(tg)
    assert await _bans(app) == []


async def test_карточка_бана_показывает_причину_и_кто_забанил(app: App, tg: Harness) -> None:
    await _ban_registered(app, 1, comment="Спам")
    await _open_section(tg, "Чёрный список")
    await _press(tg, "Участник1")
    text = _screen(tg)
    assert "Участник1 (@user1)" in text
    assert "ID: 1001" in text
    assert "Причина: вручную" in text
    assert "Комментарий: Спам" in text
    assert "Кем:" in text


async def test_карточка_автобана_показывает_что_бан_автоматический(app: App, tg: Harness) -> None:
    await app.services.bans.ban(OWNER_ID, 5555, reason=BanReason.NO_SHOW)
    await _open_section(tg, "Чёрный список")
    await _press(tg, "5555")
    assert "Причина: неявка" in _screen(tg)


async def test_разбан_с_подтверждением(app: App, tg: Harness) -> None:
    await _ban_registered(app, 1)
    await _open_section(tg, "Чёрный список")
    await _press(tg, "Участник1")
    await _press(tg, "Разбанить")
    assert "Разбанить" in _screen(tg)
    assert "Участник1" in _screen(tg)
    await _press(tg, "Да, разбанить")
    assert _alerts(tg)[-1] == "Пользователь разбанен."
    assert all(not b.active for b in await _bans(app))
    assert "Банов нет." in _screen(tg)


async def test_отказ_от_разбана_оставляет_бан(app: App, tg: Harness) -> None:
    await _ban_registered(app, 1)
    await _open_section(tg, "Чёрный список")
    await _press(tg, "Участник1")
    await _press(tg, "Разбанить")
    await _press(tg, "Нет")
    assert all(b.active for b in await _bans(app))
    assert "Причина: вручную" in _screen(tg)


async def test_разбан_уже_снятого_бана_сообщает_об_этом(app: App, tg: Harness) -> None:
    user_id = await _ban_registered(app, 1)
    await _open_section(tg, "Чёрный список")
    await _press(tg, "Участник1")
    await _press(tg, "Разбанить")
    await app.services.bans.unban(OWNER_ID, user_id)
    await _press(tg, "Да, разбанить")
    assert _alerts(tg)[-1] == "Бан уже снят."


async def test_история_банов_показывает_бан_и_разбан(app: App, tg: Harness) -> None:
    user_id = await _ban_registered(app, 1, comment="Спам")
    await app.services.bans.unban(OWNER_ID, user_id)
    await app.services.bans.ban(OWNER_ID, user_id)
    await _open_section(tg, "Чёрный список")
    await _press(tg, "Участник1")
    await _press(tg, "История")
    text = _screen(tg)
    assert "История: Участник1 (@user1)" in text
    assert "бан (вручную)" in text
    assert "Спам" in text
    assert "разбан:" in text
    assert "действует" in text
    await _press(tg, "Назад")
    assert "Причина: вручную" in _screen(tg)


async def test_назад_из_карточки_бана_возвращает_к_списку(app: App, tg: Harness) -> None:
    await _ban_registered(app, 1)
    await _open_section(tg, "Чёрный список")
    await _press(tg, "Участник1")
    await _press(tg, "Назад")
    assert "Сейчас в бане: 1." in _screen(tg)
    await _press(tg, "Назад")
    assert "Меню администратора" in _screen(tg)


async def test_админ_потерявший_права_не_открывает_карточку_бана(app: App, tg: Harness) -> None:
    await _ban_registered(app, 1)
    await _open_section(tg, "Чёрный список", ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.click(ADMIN_ID, _inline(tg), "Участник1")
    assert _alerts(tg)[-1] == "Нет доступа."


async def test_админ_потерявший_права_не_начинает_бан(app: App, tg: Harness) -> None:
    await _open_section(tg, "Чёрный список", ADMIN_ID)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.click(ADMIN_ID, _inline(tg), "Забанить")
    assert _alerts(tg)[-1] == "Нет доступа."


# ---- админы ------------------------------------------------------------------------------


async def test_список_админов_показывает_роли(tg: Harness) -> None:
    await _open_section(tg, "Админы")
    assert "Всего: 2." in _screen(tg)
    labels = _labels(tg)
    assert "Админ (@admin2) · админ" in labels
    assert any(t.endswith("· владелец") for t in labels)


async def test_владельца_удалить_нельзя_кнопки_нет(tg: Harness) -> None:
    await _open_section(tg, "Админы")
    await _press(tg, "владелец")
    assert "Роль: владелец" in _screen(tg)
    assert not any("Удалить" in t for t in _labels(tg))


async def test_подделанное_удаление_владельца_отклоняется_сервисом(app: App, tg: Harness) -> None:
    await _open_section(tg, "Админы")
    await _press(tg, "владелец")
    item_back = next(d for t, d in tg.session.buttons(_inline(tg)) if "Назад" in t)
    intent = item_back.split("\x1d", 1)[0]
    mid = _inline(tg)
    await tg.feed(
        callback_update(
            OWNER_ID,
            f"{intent}\x1dadmin_remove",
            message_id=mid,
            reply_markup=tg.session.last_markup[mid],
        )
    )
    assert OWNER_ID in await _admin_ids(app)


async def test_владелец_добавляет_админа_через_выбор_пользователя(app: App, tg: Harness) -> None:
    mid = await _open_section(tg, "Админы")
    await tg.click(OWNER_ID, mid, "Добавить админа")
    await tg.feed(_shared_update(OWNER_ID, 555, "Борис"))
    assert 555 in await _admin_ids(app)
    assert _said(tg, "Админ добавлен.")


async def test_повторное_добавление_админа_сообщает_что_он_уже_админ(app: App, tg: Harness) -> None:
    mid = await _open_section(tg, "Админы")
    await tg.click(OWNER_ID, mid, "Добавить админа")
    await tg.feed(_shared_update(OWNER_ID, ADMIN_ID, "Админ"))
    assert _said(tg, "Этот пользователь уже админ.")


async def test_владелец_удаляет_админа_с_подтверждением(app: App, tg: Harness) -> None:
    await _open_section(tg, "Админы")
    await _press(tg, "Админ (@admin2)")
    assert "Роль: админ" in _screen(tg)
    await _press(tg, "Удалить из админов")
    assert "Удалить из админов:" in _screen(tg)
    await _press(tg, "Да, удалить")
    assert _alerts(tg)[-1] == "Админ удалён."
    assert ADMIN_ID not in await _admin_ids(app)
    assert "Всего: 1." in _screen(tg)


async def test_отказ_от_удаления_админа(app: App, tg: Harness) -> None:
    await _open_section(tg, "Админы")
    await _press(tg, "Админ (@admin2)")
    await _press(tg, "Удалить из админов")
    await _press(tg, "Нет")
    assert ADMIN_ID in await _admin_ids(app)
    assert "Роль: админ" in _screen(tg)


async def test_назад_из_карточки_админа_и_из_списка(tg: Harness) -> None:
    await _open_section(tg, "Админы")
    await _press(tg, "Админ (@admin2)")
    await _press(tg, "Назад")
    assert "Всего: 2." in _screen(tg)
    await _press(tg, "Назад")
    assert "Меню администратора" in _screen(tg)


async def test_удаление_уже_удалённого_админа_показывает_ошибку(app: App, tg: Harness) -> None:
    await _open_section(tg, "Админы")
    await _press(tg, "Админ (@admin2)")
    await _press(tg, "Удалить из админов")
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await _press(tg, "Да, удалить")
    assert _alerts(tg)[-1] == "Не найдено."


async def test_потерявший_роль_владельца_получает_нет_доступа_в_админах(
    app: App, tg: Harness
) -> None:
    await _open_section(tg, "Админы")
    await app.services.admins.sync_owners([])  # владельца понизили до админа
    await _press(tg, "Админ (@admin2)")
    assert _alerts(tg)[-1] == "Нет доступа."
    assert ADMIN_ID in await _admin_ids(app)


async def test_потерявший_роль_владельца_не_добавляет_админов(app: App, tg: Harness) -> None:
    mid = await _open_section(tg, "Админы")
    await app.services.admins.sync_owners([])
    await tg.click(OWNER_ID, mid, "Добавить админа")
    assert _alerts(tg)[-1] == "Нет доступа."


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
