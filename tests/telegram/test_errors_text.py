"""Тексты ошибок сервисов для админа: общие для диалогов и обработчиков."""

import pytest

from meetup_bot.services.attendance import AlreadyMarked
from meetup_bot.services.errors import (
    DomainError,
    ErrorCode,
    InvalidOperation,
    NotFound,
    PermissionDenied,
)
from meetup_bot.services.settings_schema import SettingsValidationError
from meetup_bot.telegram.common import GENERIC_ERROR_TEXT
from meetup_bot.telegram.errors_text import (
    ERROR_TEXTS,
    NO_ACCESS,
    SEPARATELY_HANDLED_CODES,
    describe_error,
)


def test_describe_error_нет_прав() -> None:
    assert describe_error(PermissionDenied("x")) == NO_ACCESS == "Нет доступа."


def test_describe_error_не_найдено() -> None:
    assert describe_error(NotFound("встреча 5")) == "Не найдено."


def test_describe_error_явка_уже_отмечена_называет_админа() -> None:
    assert describe_error(AlreadyMarked(1, "Анна")) == "Явку уже отметил(а) Анна."


def test_describe_error_явка_уже_отмечена_без_имени() -> None:
    assert describe_error(AlreadyMarked(None, None)) == "Явку уже отметил(а) другой админ."


def test_describe_error_ошибка_настройки_показывает_сообщение_валидатора() -> None:
    error = SettingsValidationError("capacity", "Нужно целое число.")
    assert describe_error(error) == "Нужно целое число."


@pytest.mark.parametrize("code", sorted(ERROR_TEXTS))
def test_describe_error_каждый_код_invalid_operation_имеет_свой_текст(code: ErrorCode) -> None:
    text = describe_error(InvalidOperation(code))
    assert text == ERROR_TEXTS[code]
    assert text != GENERIC_ERROR_TEXT


def test_describe_error_неизвестный_код_даёт_общий_текст() -> None:
    assert describe_error(InvalidOperation("нет_такого_кода")) == GENERIC_ERROR_TEXT


def test_describe_error_произвольная_доменная_ошибка_показывает_своё_сообщение() -> None:
    assert describe_error(DomainError("особый случай")) == "особый случай"


def test_describe_error_пустая_доменная_ошибка_даёт_общий_текст() -> None:
    assert describe_error(DomainError()) == GENERIC_ERROR_TEXT


def test_describe_error_непредвиденное_исключение_не_раскрывает_детали() -> None:
    text = describe_error(RuntimeError("секретная трассировка"))
    assert text == GENERIC_ERROR_TEXT
    assert "секретная" not in text


def test_у_каждого_кода_ошибки_есть_текст_кроме_разбираемых_отдельно() -> None:
    assert set(ERROR_TEXTS) | SEPARATELY_HANDLED_CODES == set(ErrorCode)
    assert not set(ERROR_TEXTS) & SEPARATELY_HANDLED_CODES
