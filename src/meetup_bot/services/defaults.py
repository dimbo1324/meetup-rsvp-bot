"""Значения настроек по умолчанию (раздел 7 спецификации).

Схема настроек (`settings_schema`) и dataclass'ы `ScheduleSettings`, `RulesSettings`,
`RegistrationOptions` берут умолчания отсюда. Отдельный модуль нужен из-за направления
импортов: `timing` и `rules` — чистые нижние модули, а схеме нужны тексты и валидация.
"""

from __future__ import annotations

from datetime import time, timedelta

DEFAULT_TIMEZONE = "America/Montevideo"

# Встречи по умолчанию
DEFAULT_CAPACITY = 10
DEFAULT_EVENT_WEEKDAY = 4
DEFAULT_EVENT_TIME = time(16, 30)
DEFAULT_EVENT_DURATION_MINUTES = 120

# Расписание
DEFAULT_AUTO_CREATE_ENABLED = True
DEFAULT_AUTO_CREATE_DAYS_AFTER = 1
DEFAULT_AUTO_CREATE_TIME = time(10, 0)
DEFAULT_CANCEL_DEADLINE_DAYS_BEFORE = 1
DEFAULT_CANCEL_DEADLINE_TIME = time(20, 0)
DEFAULT_REGISTRATION_CLOSE_MINUTES_AFTER_START = 60
DEFAULT_REMINDER_ENABLED = True
DEFAULT_REMINDER_HOURS_BEFORE = 24
DEFAULT_ATTENDANCE_PROMPT_MINUTES_AFTER = 90
DEFAULT_POST_REMINDER_DAYS_BEFORE = 2
DEFAULT_POST_REMINDER_TIME = time(10, 0)

# Правила
DEFAULT_BAN_AFTER_NO_SHOWS = 1
DEFAULT_REMOVE_BANNED_FROM_FUTURE_EVENTS = True
DEFAULT_NOTIFY_USER_ON_BAN = False
DEFAULT_SHOW_NAMES_IN_POST = False
DEFAULT_PLUS_ONE_ENABLED = False
DEFAULT_WAITLIST_ENABLED = True
DEFAULT_LATE_PROMOTION_HOURS = 24
DEFAULT_NOTIFY_ADMINS_ON_FULL = True
DEFAULT_NOTIFY_ADMINS_ON_CANCEL = True

# Защита служебного чата от спама: не чаще одного уведомления на ключ за этот интервал
NOTIFY_CANCEL_COOLDOWN = timedelta(minutes=10)
# Повтор недоставленного напоминания «пост не разослан» — не чаще, чем раз в этот интервал
POST_REMINDER_RETRY = timedelta(minutes=30)
