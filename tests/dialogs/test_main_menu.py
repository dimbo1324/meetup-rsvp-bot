"""Главное меню админа: /menu, /cancel, права на кнопки и инструкция по рассылке."""

from typing import Any

from aiogram.types import Update

from tests.helpers import ADMIN_ID, OWNER_ID, App
from tests.tg_fake import Harness, callback_update, command_update


def _inline(tg: Harness) -> int:
    ids = [m for m, mk in tg.session.last_markup.items() if "inline_keyboard" in mk]
    return max(ids)


def _labels(tg: Harness, mid: int) -> list[str]:
    return [text for text, _ in tg.session.buttons(mid)]


def _screen(tg: Harness) -> str:
    for name, data in reversed(tg.session.calls):
        if name in {"SendMessage", "EditMessageText"} and "text" in data:
            return str(data["text"])
    return ""


def _forged(tg: Harness, user_id: int, mid: int, source_label: str, widget: str) -> Update:
    """Нажатие на кнопку, которой в окне нет: данные берём у соседней кнопки."""
    data = next(d for t, d in tg.session.buttons(mid) if source_label in t)
    intent = data.split("\x1d", 1)[0]
    return callback_update(
        user_id,
        f"{intent}\x1d{widget}",
        message_id=mid,
        reply_markup=tg.session.last_markup[mid],
    )


async def test_menu_админу_показывает_главное_меню(tg: Harness) -> None:
    await tg.feed(command_update(ADMIN_ID, "/menu"))
    sent = tg.session.of("SendMessage")[-1]
    assert sent["chat_id"] == ADMIN_ID
    assert "Меню администратора" in sent["text"]
    labels = _labels(tg, _inline(tg))
    assert any("Встречи" in t for t in labels)
    assert any("Чёрный список" in t for t in labels)
    assert any("Настройки" in t for t in labels)


async def test_menu_не_админу_отвечает_нет_доступа(tg: Harness) -> None:
    await tg.feed(command_update(777, "/menu"))
    assert tg.session.of("SendMessage")[-1]["text"] == "Нет доступа."
    assert not tg.session.last_markup


async def test_кнопка_админы_видна_только_владельцу(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    owner_labels = _labels(tg, _inline(tg))
    await tg.feed(command_update(ADMIN_ID, "/menu"))
    admin_labels = _labels(tg, _inline(tg))
    assert any("Админы" in t for t in owner_labels)
    assert not any("Админы" in t for t in admin_labels)


async def test_админ_не_может_открыть_админов_подделав_кнопку(tg: Harness) -> None:
    await tg.feed(command_update(ADMIN_ID, "/menu"))
    mid = _inline(tg)
    await tg.feed(_forged(tg, ADMIN_ID, mid, "Встречи", "admins"))
    # Скрытая кнопка игнорируется: диалог админов не открылся, окно осталось меню
    assert "Меню администратора" in _screen(tg)


async def test_кнопка_встречи_открывает_список_в_том_же_сообщении(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Встречи")
    edit = tg.session.of("EditMessageText")[-1]
    assert edit["message_id"] == mid
    assert "Встречи" in edit["text"]


async def test_как_разослать_пост_называет_бота_и_возвращает_в_меню(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    await tg.click(OWNER_ID, mid, "Как разослать пост")
    text = tg.session.of("EditMessageText")[-1]["text"]
    assert "Как разослать пост" in text
    assert "@mybot" in text
    await tg.click(OWNER_ID, mid, "В меню")
    assert "Меню администратора" in tg.session.of("EditMessageText")[-1]["text"]


async def test_cancel_сбрасывает_диалог_и_снимает_reply_клавиатуру(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    await tg.click(OWNER_ID, _inline(tg), "Встречи")
    await tg.feed(command_update(OWNER_ID, "/cancel"))
    sent = tg.session.of("SendMessage")[-1]
    assert sent["text"] == "Диалог закрыт. Меню — команда /menu."
    assert sent["reply_markup"] == {"remove_keyboard": True}


async def test_после_cancel_старые_кнопки_меню_устарели(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    mid = _inline(tg)
    # Кнопку запоминаем до /cancel: после него фейк, как Telegram, клавиатуры уже не хранит
    stale_data = next(d for t, d in tg.session.buttons(mid) if "Встречи" in t)
    stale_markup = tg.session.last_markup[mid]
    await tg.feed(command_update(OWNER_ID, "/cancel"))
    assert not tg.session.buttons(mid)
    edits_before = len(tg.session.of("EditMessageText"))
    # Старое сообщение у человека на экране могло остаться: нажатие приходит в обход Harness.click
    await tg.feed(callback_update(OWNER_ID, stale_data, message_id=mid, reply_markup=stale_markup))
    assert len(tg.session.of("EditMessageText")) == edits_before
    assert "устарело" in str(tg.session.of("AnswerCallbackQuery")[-1].get("text"))


async def test_cancel_не_админу_отвечает_нет_доступа(tg: Harness) -> None:
    await tg.feed(command_update(777, "/cancel"))
    sent = tg.session.of("SendMessage")[-1]
    assert sent["text"] == "Нет доступа."
    assert "reply_markup" not in sent


async def test_повторный_menu_заменяет_зависший_диалог(tg: Harness) -> None:
    await tg.feed(command_update(OWNER_ID, "/menu"))
    await tg.click(OWNER_ID, _inline(tg), "Встречи")
    await tg.feed(command_update(OWNER_ID, "/menu"))
    assert "Меню администратора" in _screen(tg)
    # Кнопка «Назад» нового меню не ведёт во вчерашний список: стек начат заново
    labels: list[Any] = _labels(tg, _inline(tg))
    assert not any("К списку" in t or "Назад" in t for t in labels)


async def test_не_админ_не_может_нажать_кнопку_чужого_меню(app: App, tg: Harness) -> None:
    await tg.feed(command_update(ADMIN_ID, "/menu"))
    mid = _inline(tg)
    await app.services.admins.remove_admin(OWNER_ID, ADMIN_ID)
    await tg.click(ADMIN_ID, mid, "Встречи")
    alerts = [d for d in tg.session.of("AnswerCallbackQuery") if d.get("text")]
    assert alerts[-1]["text"] == "Нет доступа."
    assert alerts[-1]["show_alert"] is True
