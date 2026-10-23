"""Проверка полей встречи при создании и правке (5.2, 5.10).

Правила текстовых полей берутся из схемы настроек, чтобы лимиты в админке и во встрече
не разъехались; здесь только перевод ошибок схемы в `InvalidOperation` с понятным кодом.
"""

from __future__ import annotations

from meetup_bot.services.errors import InvalidOperation
from meetup_bot.services.settings_schema import (
    SettingsValidationError,
    check_html,
    get_spec,
    validate,
)

MIN_CAPACITY = 1
MAX_CAPACITY = 200
PLACE_URL_MAX_LEN = 2000
PHOTO_FILE_ID_MAX_LEN = 256


def validate_capacity(value: int) -> int:
    # bool — подкласс int, но True как лимит мест — ошибка ввода
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidOperation("invalid_capacity", "Лимит мест должен быть целым числом.")
    if not MIN_CAPACITY <= value <= MAX_CAPACITY:
        raise InvalidOperation(
            "invalid_capacity", f"Лимит мест — от {MIN_CAPACITY} до {MAX_CAPACITY}."
        )
    return value


def validate_description(value: str) -> list[str]:
    """Проверяет HTML описания; возвращает предупреждения (например, о длине >700)."""
    try:
        return check_html("description_html", value)
    except SettingsValidationError as exc:
        raise InvalidOperation("invalid_description_html", exc.message) from exc


def validate_place(value: str) -> str:
    try:
        normalized, _ = validate(get_spec("default_place"), value)
    except SettingsValidationError as exc:
        raise InvalidOperation("invalid_place", exc.message) from exc
    return str(normalized)


def validate_url(value: str) -> str:
    """Пусто либо https:// без пробелов и управляющих символов, не длиннее 2000."""
    if len(value) > PLACE_URL_MAX_LEN:
        raise InvalidOperation(
            "invalid_place_url", f"Ссылка не длиннее {PLACE_URL_MAX_LEN} символов."
        )
    if any(not ch.isprintable() for ch in value.strip()):
        raise InvalidOperation("invalid_place_url", "В ссылке не должно быть управляющих символов.")
    try:
        normalized, _ = validate(get_spec("default_place_url"), value)
    except SettingsValidationError as exc:
        raise InvalidOperation("invalid_place_url", exc.message) from exc
    return str(normalized)


def validate_photo(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.strip() or len(value) > PHOTO_FILE_ID_MAX_LEN:
        raise InvalidOperation(
            "invalid_photo_file_id",
            f"Идентификатор фото — непустая строка до {PHOTO_FILE_ID_MAX_LEN} символов.",
        )
    return value
