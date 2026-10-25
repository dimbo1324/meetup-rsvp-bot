# 7. Настройки

Хранятся в таблице `settings` (ключ → JSON-значение). Значения по умолчанию определены в коде (`settings/defaults.py`). В БД лежат только изменённые значения. Любое изменение пишется в `audit_log`.

| Ключ | Тип | По умолчанию | Ограничения | Описание |
|---|---|---|---|---|
| `capacity` | int | 10 | 1–200 | Лимит мест для новых встреч |
| `event_weekday` | int | 5 (сб) | 0–6, пн = 0 | День встречи. Остальные дни расписания считаются от него («день до», «день после»), а не от дней недели |
| `event_time` | HH:MM | 17:00 ⚠️ уточнить | — | Время начала |
| `default_place` | str | «» | ≤ 200 | Место по умолчанию |
| `default_place_url` | url | «» | https:// | Ссылка на карту |
| `default_description` | html | «» | см. 5.10 | Текст поста по умолчанию |
| `default_photo_file_id` | str | null | — | Фото по умолчанию |
| `auto_create_enabled` | bool | true | — | Автосоздание встреч |
| `auto_create_days_after` | int | 1 | 0–6 | Через сколько дней после встречи создавать следующую («день после») |
| `auto_create_time` | HH:MM | 10:00 | — | Во сколько создавать |
| `cancel_deadline_days_before` | int | 1 | 0–7 | Дедлайн отмены: за сколько дней |
| `cancel_deadline_time` | HH:MM | 20:00 | — | Дедлайн отмены: во сколько |
| `registration_close_hours_before` | int | 0 | 0–72 | Закрытие записи за N часов до начала. 0 — автозакрытия нет, запись идёт до начала встречи; админ закрывает вручную в любой момент |
| `reminder_enabled` | bool | true | — | Напоминания в личку |
| `reminder_hours_before` | int | 24 | 1–72 | Когда напоминать |
| `attendance_prompt_hours_after` | int | 5 | 1–48 | Когда просить админов отметить явку |
| `ban_after_no_shows` | int | 1 | 0–10 | Порог бана, 0 = автобан выключен |
| `remove_banned_from_future_events` | bool | true | — | Снимать забаненного с будущих встреч |
| `notify_user_on_ban` | bool | false | — | Сообщать человеку о бане |
| `show_names_in_post` | bool | false | — | Имена в посте или только счётчик и очередь. Админы видят имена в своих списках всегда |
| `plus_one_enabled` | bool | false | — | Кнопка «+1» |
| `waitlist_enabled` | bool | true | — | Лист ожидания |
| `late_promotion_hours` | int | 24 | 0–72 | См. 5.5 |
| `notify_admins_on_full` | bool | true | — | Уведомлять о заполнении |
| `notify_admins_on_cancel` | bool | true | — | Уведомлять об отменах |
| `timezone` | IANA | America/Montevideo | валидный `zoneinfo` | Часовой пояс |
| `text.*` | str | раздел 6 | ≤ 200 для всплывающих окон | Тексты |

Настройки влияют только на **новые** встречи. Параметры уже созданной встречи меняются в её карточке.
