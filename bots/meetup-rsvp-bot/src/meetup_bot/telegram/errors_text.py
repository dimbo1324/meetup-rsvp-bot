"""Понятные админу тексты ошибок сервисов: общие для диалогов и обработчиков."""

from __future__ import annotations

import logging

from meetup_bot.services.attendance import ATTENDANCE_WINDOW, AlreadyMarked
from meetup_bot.services.errors import (
    DomainError,
    ErrorCode,
    InvalidOperation,
    NotFound,
    PermissionDenied,
)
from meetup_bot.services.settings_schema import (
    MAX_CAPACITY,
    MIN_CAPACITY,
    PLACE_MAX_LEN,
    SettingsValidationError,
)
from meetup_bot.telegram.common import GENERIC_ERROR_TEXT

logger = logging.getLogger(__name__)

NO_ACCESS = "Нет доступа."
NOT_FOUND = "Не найдено."
# Подпись, когда имя отметившего явку админа неизвестно
ANOTHER_ADMIN = "другой админ"

ERROR_TEXTS: dict[ErrorCode, str] = {
    ErrorCode.ALREADY_ADMIN: "Этот пользователь уже админ.",
    ErrorCode.ALREADY_REGISTERED: "Этот человек уже записан на встречу.",
    ErrorCode.ATTENDANCE_ALREADY_MARKED: (
        "Явка по этой встрече уже отмечена: переносить или заново открывать запись нельзя."
    ),
    ErrorCode.ATTENDANCE_NOT_YET: "Отметить явку можно после закрытия записи на встречу.",
    ErrorCode.ATTENDANCE_WINDOW_CLOSED: (
        f"Окно отметки явки закрыто (прошло более {ATTENDANCE_WINDOW.days} суток)."
    ),
    ErrorCode.BAD_STATUS: "Это действие недоступно для встречи в текущем статусе.",
    ErrorCode.CANNOT_BAN_ADMIN: "Нельзя банить админа, владельца или самого себя.",
    ErrorCode.CANNOT_REMOVE_OWNER: "Владельца нельзя удалить.",
    ErrorCode.CAPACITY_BELOW_TAKEN: (
        "Лимит не может быть ниже числа занятых мест. Сначала уберите лишних."
    ),
    ErrorCode.EVENT_CANCELLED: "Встреча отменена.",
    ErrorCode.INVALID_ATTENDANCE: "Недопустимая отметка явки.",
    ErrorCode.INVALID_CAPACITY: (
        f"Лимит мест должен быть числом от {MIN_CAPACITY} до {MAX_CAPACITY}."
    ),
    ErrorCode.INVALID_DESCRIPTION_HTML: "Описание содержит недопустимую HTML-разметку.",
    ErrorCode.INVALID_PLACE: (
        f"Недопустимое название места (до {PLACE_MAX_LEN} символов, без управляющих символов)."
    ),
    ErrorCode.INVALID_PLACE_URL: "Ссылка должна начинаться с https:// и быть без пробелов.",
    ErrorCode.INVALID_PHOTO_FILE_ID: "Недопустимое фото.",
    ErrorCode.INVALID_TIMES: "Моменты времени не согласованы: проверьте даты и порядок.",
    ErrorCode.INVALID_USER: "Недопустимый пользователь.",
    ErrorCode.NO_SHOW_REQUIRES_ACTIVE: "Отметить как неявку можно только активную запись.",
    ErrorCode.NOT_LIVE: "Эта запись уже не активна.",
    ErrorCode.NOT_MARKED: "Явка ещё не отмечена.",
    ErrorCode.OVER_CAPACITY: "Мест не хватает. Подтвердите превышение лимита.",
    ErrorCode.REGISTRATION_TIME_PASSED: (
        "Время записи уже вышло. Чтобы открыть запись, сначала передвиньте время закрытия "
        "в «Моментах»."
    ),
    ErrorCode.UNKNOWN_REGISTRATION: "Запись не найдена.",
    ErrorCode.WHO_REQUIRED: "Укажите пользователя или имя участника.",
}

# Коды возникают только при записи кнопкой под постом: текст выбирает обработчик кнопок
SEPARATELY_HANDLED_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.INVALID_GUESTS, ErrorCode.PLUS_ONE_DISABLED}
)


def describe_error(error: BaseException) -> str:
    """Понятный админу текст для исключения сервиса (трейсбеки не показываем)."""
    if isinstance(error, PermissionDenied):
        return NO_ACCESS
    if isinstance(error, NotFound):
        return NOT_FOUND
    if isinstance(error, AlreadyMarked):
        who = error.marked_by_name or ANOTHER_ADMIN
        return f"Явку уже отметил(а) {who}."
    if isinstance(error, SettingsValidationError):
        return error.message
    if isinstance(error, InvalidOperation):
        return ERROR_TEXTS.get(error.code, GENERIC_ERROR_TEXT)
    if isinstance(error, DomainError):
        return str(error) or GENERIC_ERROR_TEXT
    logger.error("непредвиденная ошибка в диалоге", exc_info=error)
    return GENERIC_ERROR_TEXT
