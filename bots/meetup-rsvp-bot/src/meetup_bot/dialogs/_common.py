"""Общие помощники диалогов: доступ к сервисам, права, уведомления в окне, тексты ошибок."""

from __future__ import annotations

import html

from aiogram import Bot
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import DialogManager

from meetup_bot.clock import Clock
from meetup_bot.container import Services
from meetup_bot.services.errors import DomainError, InvalidOperation
from meetup_bot.telegram.attendance_ui import AttendanceUI
from meetup_bot.telegram.errors_text import NO_ACCESS, describe_error
from meetup_bot.user_info import UserInfo

NOTICE_KEY = "notice"
WARNING_PREFIX = "⚠️ "
# Создание или перенос поздно: дедлайн отмены приравнен к моменту действия (5.2)
DEADLINE_CLAMPED_WARNING = (
    "Время отмены записи уже прошло, поэтому отменить запись без штрафа участники не смогут."
)


def get_services(manager: DialogManager) -> Services:
    services: Services = manager.middleware_data["services"]
    return services


def get_clock(manager: DialogManager) -> Clock:
    clock: Clock = manager.middleware_data["clock"]
    return clock


def get_bot(manager: DialogManager) -> Bot:
    bot: Bot = manager.middleware_data["bot"]
    return bot


def get_attendance_ui(manager: DialogManager) -> AttendanceUI:
    ui: AttendanceUI = manager.middleware_data["attendance_ui"]
    return ui


def actor_id(manager: DialogManager) -> int:
    """ID пользователя, нажавшего кнопку (не чата)."""
    user = manager.middleware_data["event_from_user"]
    return int(user.id)


async def is_admin(manager: DialogManager) -> bool:
    """Права перепроверяются на каждом действии: скрытая кнопка защитой не считается."""
    return await get_services(manager).users.is_admin(actor_id(manager))


async def guard(event: Message | CallbackQuery, manager: DialogManager, *, close: bool) -> bool:
    """True, если у нажавшего есть права; иначе показывает отказ и, при `close`, закрывает диалог.

    Закрывают диалоги, где продолжать без прав нельзя (мастер, участники); в остальных окнах
    достаточно отказа, чтобы человек мог вернуться в меню сам.
    """
    if await is_admin(manager):
        return True
    if isinstance(event, CallbackQuery):
        await event.answer(NO_ACCESS, show_alert=True)
    else:
        await event.answer(NO_ACCESS)
    if close:
        await manager.done()
    return False


# ---- уведомление над текстом окна ----------------------------------------------------


def set_notice(manager: DialogManager, text: str | None) -> None:
    """Запоминает уведомление для следующего показа окна; None снимает прежнее."""
    manager.dialog_data[NOTICE_KEY] = text


def pop_notice(manager: DialogManager) -> str:
    """Уведомление «на один показ»: возвращается и сразу забывается."""
    text = manager.dialog_data.pop(NOTICE_KEY, None)
    return str(text) if text else ""


def notice_block(manager: DialogManager, prefix: str = "") -> str:
    """Экранированное уведомление отдельным абзацем; пусто, если уведомления нет.

    Не забывает уведомление: оно висит, пока его не заменят или не снимут (`set_notice(None)`).
    """
    text = manager.dialog_data.get(NOTICE_KEY)
    return f"\n{prefix}{html.escape(str(text), quote=False)}\n" if text else ""


# ---- ошибки ---------------------------------------------------------------------------


def error_text(error: DomainError) -> str:
    """Текст для окна: описание кода ошибки и, если сервис дал, уточнение.

    Уточнение («Некорректный HTML: тег <b> не закрыт», «Не длиннее 200 символов») не должно
    теряться, а во всплывающем окне (лимит 200 символов) его не показывают: там `describe_error`.
    """
    base = describe_error(error)
    if not isinstance(error, InvalidOperation):
        return base
    detail = str(error).strip()
    if not detail or detail == error.code.value or detail.casefold() in base.casefold():
        return base
    return f"{base} {detail}"


# ---- выбор пользователя ---------------------------------------------------------------


def picked_user(result: object) -> UserInfo | None:
    """Пользователь из результата диалога выбора; None, если выбор отменён или результат чужой."""
    if not isinstance(result, dict) or result.get("user_id") is None:
        return None
    return UserInfo(
        id=int(result["user_id"]),
        first_name=str(result.get("first_name") or ""),
        last_name=result.get("last_name"),
        username=result.get("username"),
    )
