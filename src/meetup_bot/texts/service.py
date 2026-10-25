"""Сообщения бота в служебный чат админов.

В отличие от текстов `text.*` (раздел 6), они не настраиваются из меню: это служебная
информация для админов, а не обращение к участникам.
"""

from __future__ import annotations

POST_SHARE_HINT = "в нужном чате наберите @ и имя бота и выберите встречу."
AUTO_CREATED_TEXT = "Создана встреча на {when}. Разошлите пост: " + POST_SHARE_HINT
POST_REMINDER_TEXT = "Пост встречи {when} ещё не разослан. Чтобы разослать: " + POST_SHARE_HINT

EVENT_FULL_TEXT = "Встреча {when} заполнена: занято {taken} из {capacity}."
USER_CANCELLED_TEXT = "{name} отменил(а) запись на встречу {when}. Занято {taken} из {capacity}."
CLOSED_SUMMARY_HEAD = "Запись на встречу ({when}) закрыта."
CLOSED_SUMMARY_FILLED = "Заполнено {taken} из {capacity}."
ATTENDANCE_NOT_MARKED_TEXT = "Никто ещё не отметил явку на встрече ({when})."
SCHEDULER_STOPPED_TEXT = "⚠️ Планировщик остановился: {error}"
BOT_STARTED_TEXT = "Бот запущен, версия {version}"
ERROR_REPORT_TEXT = "⚠️ Ошибка: {kind}"
BACKUP_CAPTION = "Резервная копия базы: {name}"
