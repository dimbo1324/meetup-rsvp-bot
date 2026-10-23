"""Общие помощники диалогов: доступ к сервисам, проверка прав, тексты ошибок."""

from __future__ import annotations

import logging
from typing import Any

from aiogram_dialog import DialogManager
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.container import Services
from meetup_bot.handlers._common import check_admin
from meetup_bot.services.attendance import AlreadyMarked
from meetup_bot.services.errors import DomainError, InvalidOperation, NotFound, PermissionDenied
from meetup_bot.services.settings_schema import SettingsValidationError

logger = logging.getLogger(__name__)

NO_ACCESS = "Нет доступа."
GENERIC_ERROR = "Что-то пошло не так, попробуйте ещё раз."

# Человеческие тексты для кодов InvalidOperation
ERROR_TEXTS: dict[str, str] = {
    "already_admin": "Этот пользователь уже админ.",
    "already_registered": "Этот человек уже записан на встречу.",
    "already_started": "Встреча уже началась.",
    "attendance_window_closed": "Окно отметки явки закрыто (прошло более 7 суток).",
    "bad_status": "Это действие недоступно для встречи в текущем статусе.",
    "cannot_ban_admin": "Нельзя банить админа, владельца или самого себя.",
    "cannot_remove_owner": "Владельца нельзя удалить.",
    "capacity_below_taken": "Лимит не может быть ниже числа занятых мест. Сначала уберите лишних.",
    "event_cancelled": "Встреча отменена.",
    "event_not_started": "Встреча ещё не началась: отметить явку пока нельзя.",
    "invalid_attendance": "Недопустимая отметка явки.",
    "invalid_capacity": "Лимит мест должен быть числом от 1 до 200.",
    "invalid_description_html": "Описание содержит недопустимую HTML-разметку.",
    "invalid_place": "Недопустимое название места (до 200 символов, без управляющих символов).",
    "invalid_place_url": "Ссылка должна начинаться с https:// и быть без пробелов.",
    "invalid_photo_file_id": "Недопустимое фото.",
    "invalid_times": "Моменты времени не согласованы: проверьте даты и порядок.",
    "invalid_user": "Недопустимый пользователь.",
    "no_show_requires_active": "Отметить как неявку можно только активную запись.",
    "not_live": "Эта запись уже не активна.",
    "not_marked": "Явка ещё не отмечена.",
    "over_capacity": "Мест не хватает. Подтвердите превышение лимита.",
    "unknown_registration": "Запись не найдена.",
    "who_required": "Укажите пользователя или имя участника.",
}


def describe_error(error: BaseException) -> str:
    """Понятный админу текст для исключения сервиса (трейсбеки не показываем)."""
    if isinstance(error, PermissionDenied):
        return NO_ACCESS
    if isinstance(error, NotFound):
        return "Не найдено."
    if isinstance(error, AlreadyMarked):
        who = error.marked_by_name or "другой админ"
        return f"Явку уже отметил(а) {who}."
    if isinstance(error, SettingsValidationError):
        return error.message
    if isinstance(error, InvalidOperation):
        return ERROR_TEXTS.get(error.code, GENERIC_ERROR)
    if isinstance(error, DomainError):
        return str(error) or GENERIC_ERROR
    logger.error("непредвиденная ошибка в диалоге", exc_info=error)
    return GENERIC_ERROR


def get_services(manager: DialogManager) -> Services:
    services: Services = manager.middleware_data["services"]
    return services


def get_sessions(manager: DialogManager) -> async_sessionmaker[AsyncSession]:
    sessions: async_sessionmaker[AsyncSession] = manager.middleware_data["sessions"]
    return sessions


def actor_id(manager: DialogManager) -> int:
    """ID пользователя, нажавшего кнопку (не чата)."""
    user = manager.middleware_data["event_from_user"]
    return int(user.id)


async def is_admin(manager: DialogManager) -> bool:
    """Права перепроверяются на каждом действии: скрытая кнопка защитой не считается."""
    return await check_admin(get_sessions(manager), actor_id(manager))


def middleware_value(manager: DialogManager, key: str) -> Any:
    return manager.middleware_data[key]
