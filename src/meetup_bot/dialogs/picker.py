"""Выбор пользователя Telegram: системный выбор, пересланное сообщение, список знакомых.

Контракт запуска и результата описан в docstring `dialogs/states.py`.
"""

from __future__ import annotations

import html
from typing import Any

from aiogram.enums import ContentType, MessageOriginType
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    Message,
    MessageOriginUser,
)
from aiogram_dialog import Dialog, DialogManager, ShowMode, Window
from aiogram_dialog.api.internal import RawKeyboard
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Column, Keyboard, Row, Select
from aiogram_dialog.widgets.markup.reply_keyboard import ReplyKeyboardFactory
from aiogram_dialog.widgets.text import Const, Format

from meetup_bot.dialogs._common import NO_ACCESS, get_sessions, is_admin
from meetup_bot.dialogs.states import PickerSG
from meetup_bot.repositories import users_known

PAGE_SIZE = 8
_REQUEST_ID = 1
_DEFAULT_TITLE = "Выберите пользователя"
_HINT = "Нажмите кнопку «Выбрать пользователя» или перешлите сюда любое сообщение этого человека."


class RequestUsers(Keyboard):
    """Reply-кнопка системного выбора пользователя (по образцу `RequestContact`).

    Работает только в личке и только в reply-клавиатуре, поэтому окно с ней использует
    `ReplyKeyboardFactory`; обычные кнопки окна библиотека сама превращает в reply-кнопки.
    """

    def __init__(self, text: str, request_id: int = _REQUEST_ID, when: Any = None) -> None:
        super().__init__(when=when)
        self._text = text
        self._request_id = request_id

    async def _render_keyboard(self, data: dict[str, Any], manager: DialogManager) -> RawKeyboard:
        return [
            [
                KeyboardButton(
                    text=self._text,
                    request_users=KeyboardButtonRequestUsers(
                        request_id=self._request_id,
                        user_is_bot=False,
                        max_quantity=1,
                        request_name=True,
                        request_username=True,
                    ),
                )
            ]
        ]


def _start_data(manager: DialogManager) -> dict[str, Any]:
    data = manager.start_data
    return data if isinstance(data, dict) else {}


def _exclude_ids(manager: DialogManager) -> set[int]:
    return {int(i) for i in _start_data(manager).get("exclude_ids") or []}


def _result(
    user_id: int, first_name: str | None, last_name: str | None, username: str | None
) -> dict[str, Any]:
    # Telegram не всегда отдаёт имя (приватность): подставляем то, что можно показать
    name = first_name or (f"@{username}" if username else "Без имени")
    return {"user_id": user_id, "first_name": name, "last_name": last_name, "username": username}


async def _finish(manager: DialogManager, result: dict[str, Any] | None) -> None:
    # SEND обязателен: только отправка нового сообщения даёт библиотеке снять reply-клавиатуру
    # (при редактировании или удалении сообщения клавиатура в Telegram остаётся)
    await manager.done(result=result, show_mode=ShowMode.SEND)


async def _hint(message: Message, manager: DialogManager, text: str) -> None:
    await message.answer(text)
    manager.show_mode = ShowMode.NO_UPDATE


async def _accept(
    message: Message, manager: DialogManager, user: dict[str, Any], is_bot: bool = False
) -> None:
    if is_bot:
        await _hint(message, manager, "Ботов выбирать нельзя.")
    elif user["user_id"] in _exclude_ids(manager):
        await _hint(message, manager, "Этого пользователя выбрать нельзя.")
    else:
        await _finish(manager, user)


async def on_message(message: Message, widget: MessageInput, manager: DialogManager) -> None:
    if not await is_admin(manager):
        await _hint(message, manager, NO_ACCESS)
        return
    if message.users_shared and message.users_shared.users:
        shared = message.users_shared.users[0]
        user = _result(shared.user_id, shared.first_name, shared.last_name, shared.username)
        await _accept(message, manager, user)
        return
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        sender = origin.sender_user
        user = _result(sender.id, sender.first_name, sender.last_name, sender.username)
        await _accept(message, manager, user, is_bot=bool(sender.is_bot))
        return
    if origin is not None and origin.type == MessageOriginType.HIDDEN_USER:
        await _hint(
            message,
            manager,
            "Профиль этого человека скрыт настройками приватности, по пересланному сообщению "
            "его не определить. Выберите его кнопкой «Выбрать пользователя» или из списка.",
        )
        return
    await _hint(message, manager, _HINT)


async def on_known(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    if not await is_admin(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    manager.dialog_data["page"] = 0
    # Новое сообщение вместо редактирования: так библиотека снимет reply-клавиатуру
    await manager.switch_to(PickerSG.known, show_mode=ShowMode.SEND)


async def on_cancel(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await _finish(manager, None)


async def on_back(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.switch_to(PickerSG.pick, show_mode=ShowMode.SEND)


async def pick_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    title = str(_start_data(dialog_manager).get("title") or _DEFAULT_TITLE)
    return {"text": f"<b>{html.escape(title)}</b>\n\n{_HINT}"}


async def known_getter(dialog_manager: DialogManager, **kwargs: Any) -> dict[str, Any]:
    if not await is_admin(dialog_manager):
        return {"text": NO_ACCESS, "users": [], "has_prev": False, "has_next": False}
    exclude = _exclude_ids(dialog_manager)
    page = int(dialog_manager.dialog_data.get("page", 0))
    async with get_sessions(dialog_manager)() as session:
        total = await users_known.count_known(session, exclude)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        dialog_manager.dialog_data["page"] = page
        rows = await users_known.list_known(session, PAGE_SIZE, page * PAGE_SIZE, exclude)
    users = []
    for row in rows:
        label = " ".join(p for p in (row.first_name, row.last_name) if p)
        if row.username:
            label += f" (@{row.username})"
        users.append({"id": row.id, "label": label})
    text = (
        f"Известные пользователи, страница {page + 1} из {pages}."
        if total
        else "Пока нет известных пользователей."
    )
    return {"text": text, "users": users, "has_prev": page > 0, "has_next": page < pages - 1}


async def on_user_chosen(
    callback: CallbackQuery, widget: Any, manager: DialogManager, item_id: int
) -> None:
    if not await is_admin(manager):
        await callback.answer(NO_ACCESS, show_alert=True)
        return
    async with get_sessions(manager)() as session:
        found = await users_known.get_many(session, [item_id])
    user = found.get(item_id)
    if user is None or item_id in _exclude_ids(manager):
        await callback.answer("Этого пользователя выбрать нельзя.", show_alert=True)
        return
    await manager.done(result=_result(user.id, user.first_name, user.last_name, user.username))


async def on_prev(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data["page"] = max(0, int(manager.dialog_data.get("page", 0)) - 1)


async def on_next(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    manager.dialog_data["page"] = int(manager.dialog_data.get("page", 0)) + 1


async def on_known_cancel(callback: CallbackQuery, button: Button, manager: DialogManager) -> None:
    await manager.done()


DIALOGS = [
    Dialog(
        Window(
            Format("{text}"),
            RequestUsers("👤 Выбрать пользователя"),
            Button(Const("📋 Из списка записывавшихся"), id="known", on_click=on_known),
            Button(Const("✖ Отмена"), id="cancel", on_click=on_cancel),
            MessageInput(on_message, content_types=ContentType.ANY),
            markup_factory=ReplyKeyboardFactory(resize_keyboard=True),
            getter=pick_getter,
            state=PickerSG.pick,
            parse_mode="HTML",
        ),
        Window(
            Format("{text}"),
            Column(
                Select(
                    Format("{item[label]}"),
                    id="known_user",
                    item_id_getter=lambda item: item["id"],
                    items="users",
                    type_factory=int,
                    on_click=on_user_chosen,
                )
            ),
            Row(
                Button(Const("◀"), id="prev", on_click=on_prev, when="has_prev"),
                Button(Const("▶"), id="next", on_click=on_next, when="has_next"),
            ),
            Button(Const("⬅ Назад"), id="back", on_click=on_back),
            Button(Const("✖ Отмена"), id="known_cancel", on_click=on_known_cancel),
            getter=known_getter,
            state=PickerSG.known,
            parse_mode="HTML",
        ),
    )
]
