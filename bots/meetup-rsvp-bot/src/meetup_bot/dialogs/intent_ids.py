"""Надёжные идентификаторы окон aiogram-dialog.

ОБХОДНОЕ РЕШЕНИЕ для aiogram-dialog 2.6: `new_int_id()` строит id из текущей секунды и
случайного числа 0..99, то есть внутри одной секунды возможны лишь 100 разных id. Два окна,
открытые в одну секунду (например, чёрный список и сразу выбор пользователя), с вероятностью
1% получают одинаковый id — стек ломается («Stack is broken … resetting»), и нажатия теряются.
Подменяем генератор на 40-битное случайное число: id остаётся коротким (≈7 символов base62),
поэтому `callback_data` укладывается в лимит 64 байта. Убрать, когда библиотека это исправит.
"""

from __future__ import annotations

import secrets

from aiogram_dialog.api.entities import stack

INTENT_ID_BITS = 40


def secure_int_id() -> int:
    # Ноль кодируется одним символом и выглядит как «пустой» id, поэтому исключаем его
    return secrets.randbits(INTENT_ID_BITS) or 1


def install() -> None:
    """Подменяет генератор id; идемпотентно."""
    stack.new_int_id = secure_int_id
