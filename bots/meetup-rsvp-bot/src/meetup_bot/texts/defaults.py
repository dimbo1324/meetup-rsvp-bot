"""Тексты бота по умолчанию (раздел 6 спецификации).

В БД хранятся только изменённые админом тексты; всё остальное берётся отсюда.
"""

from __future__ import annotations

TEXT_PREFIX = "text."

# Допустимые подстановки в текстах; любое другое имя в `{...}` отклоняется при сохранении
ALLOWED_PLACEHOLDERS: frozenset[str] = frozenset(
    {"date", "time", "end_time", "weekday", "place", "deadline", "position", "name", "reason"}
)

# Тексты-сообщения (уходят обычным сообщением в HTML, а не во всплывающее окно): для них
# нет лимита 200 символов, зато подставляемые значения экранируются под parse_mode=HTML
MESSAGE_TEXT_KEYS: frozenset[str] = frozenset(
    {
        "text.reminder",
        "text.promoted",
        "text.event_cancelled",
        "text.event_moved",
        "text.attendance_marked_by",
        "text.ban_notice",
    }
)

DEFAULT_TEXTS: dict[str, str] = {
    "text.registered": (
        "Вы записаны на встречу: {weekday}, {date}, {time}–{end_time}. "
        "Если планы изменятся, отмените запись до {deadline}."
    ),
    "text.registered_late": (
        "Вы записаны на встречу: {weekday}, {date}, {time}–{end_time}. "
        "Обратите внимание: отменить запись уже нельзя."
    ),
    "text.full": "Извините, мест не осталось. Попробуйте в следующий раз!",
    "text.waitlisted": (
        "Мест нет, вы в листе ожидания под номером {position}. "
        "Если место освободится, бот напишет вам."
    ),
    "text.already": "Вы уже записаны.",
    "text.already_waitlist": "Вы уже в листе ожидания, номер {position}.",
    "text.only_one_seat": "Осталось одно место — запишитесь без гостя.",
    "text.closed": "Запись на эту встречу закрыта.",
    "text.banned": (
        "К сожалению, вы не можете записываться на встречи. "
        "Если это ошибка, напишите администратору."
    ),
    "text.cancelled": "Запись отменена. Спасибо, что предупредили!",
    "text.cancel_too_late": (
        "Отменить запись уже нельзя. Если не можете прийти, напишите администратору."
    ),
    "text.not_registered": "Вы не были записаны на эту встречу.",
    "text.enable_dm_hint": "Нажмите «Старт» у бота, чтобы получать напоминания.",
    "text.reminder": (
        "Напоминаем о встрече: {weekday}, {date}, {time}–{end_time}, {place}. "
        "Не сможете прийти? Отмените запись до {deadline}."
    ),
    "text.promoted": (
        "Освободилось место — вы записаны на встречу: {weekday}, {date}, {time}–{end_time}!"
    ),
    "text.event_cancelled": "Встреча {date} отменена. {reason}",
    "text.event_moved": "Встреча перенесена: теперь {weekday}, {date}, {time}–{end_time}, {place}.",
    "text.attendance_marked_by": "Явку отметил(а) {name}",
    "text.ban_notice": (
        "Вы больше не можете записываться на встречи. Если это ошибка, напишите администратору."
    ),
}
