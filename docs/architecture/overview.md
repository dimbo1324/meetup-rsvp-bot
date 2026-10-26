# Текущее состояние системы

Что **реально построено** прямо сейчас. Намерение и целевая архитектура — `docs/spec/`;
порядок работ — `docs/ROADMAP.md`. Если этот документ расходится с кодом, прав код: исправьте
документ в той же задаче.

_Обновлено: 2026-10-26 (ветка `feat/m6-2-launch-ready`: вехи M6.1 и M6.2 и три волны аудита готовы; бот не проверялся на реальном Telegram, `deploy.yml` нет)._

Одна установка — один процесс на `asyncio`, SQLite, long polling. Слои: `handlers/`, `dialogs/` →
`services/` → `repositories/` → `db/`; рядом `renderer/`, `telegram/`, `scheduler/`, `middlewares/`,
`texts/`. Корень сборки — `container.py`, запуск — `app.py`.

## Что есть

| Область | Состояние | Где |
|---|---|---|
| Конфигурация | `Settings` из окружения: `BOT_TOKEN` (`SecretStr`, проверка формата), `OWNER_IDS`, `SERVICE_CHAT_ID`, `DATABASE_URL`, `ENV` (по умолчанию `prod`), `LOG_LEVEL`, `SENTRY_DSN`, `APP_VERSION` (версия сборки образа). Коды выхода: 0 — штатно, 1 — токен отклонён или нет сети, 2 — неверный `.env` | `config.py`, `__main__.py` |
| Время | `Clock`: `SystemClock`, `FakeClock` (тесты), `ShiftableClock` (только `ENV=dev`, команда `/dev_shift`) | `clock.py` |
| Данные пользователя | `UserInfo` — нейтральное значение, которое ходит между слоями | `user_info.py` |
| БД: основа | `Base`, `UtcDateTime` (aware-UTC при чтении и записи), движок с PRAGMA (WAL, `busy_timeout`, `foreign_keys`, `synchronous`); каждая транзакция начинается с `BEGIN IMMEDIATE` | `db/` |
| БД: модели и миграции | 8 таблиц (`users`, `admins`, `events`, `registrations`, `event_posts`, `bans`, `settings`, `audit_log`), частичные уникальные индексы, CHECK-ограничения; наборы статусов (`LIVE_REGISTRATION_STATUSES`, `UNFINISHED_EVENT_STATUSES`) — в `db/models.py`. Миграции `0001`–`0006` (`0005` добавляет колонки прямым `ALTER TABLE`, откат — нативным `DROP COLUMN`: пересоздание `events` падало бы на внешних ключах базы с данными) | `db/models.py`, `alembic/versions/` |
| Репозитории | `admins`, `audit`, `bans`, `event_posts`, `events`, `events_admin`, `registrations`, `schedule` (выборки для планировщика), `settings`, `users`, `users_known` | `repositories/` |
| Время встречи | Расчёт моментов 5.2: начало, конец, дедлайн отмены, закрытие записи, напоминание, запрос явки; ближайшая дата; приравнивание прошедшего дедлайна к моменту создания; инварианты порядка | `services/timing.py` |
| Умолчания | Все значения по умолчанию в одном месте: пятница 16:30, 120 минут, автосоздание `+1` день 10:00, закрытие записи `+60` минут, явка `+90` минут, напоминание о посте `-2` дня 10:00 | `services/defaults.py` |
| Встречи | Создание (`create_event_as_admin` — с проверкой прав, `create_event` — системное), правка, перенос с пересчётом моментов, лимит, закрытие и открытие записи, отмена, ручная правка моментов, список (открытые и за 14 дней), карточка, шаблон новой встречи по прошлой; значения-результаты для интерфейса (`EventCard`, `CreatedEvent`, `RescheduleResult`) | `services/events.py`, `event_results.py`, `event_validation.py`, `event_template.py` |
| Запись | `register`/`cancel` под `asyncio.Lock` встречи и `BEGIN IMMEDIATE`; запись открыта и после начала до закрытия; лист ожидания; «+1» (выключен); `my_registrations` для `/start remind`; фоновые уведомления (`BackgroundTasks`); ограничитель уведомлений в служебный чат (`Cooldown`) | `services/registration.py`, `waitlist.py`, `cooldown.py`, `safe.py` |
| Админы и участники | Роли, проверка прав в каждой операции (`require_admin`/`require_owner`, `load_event_for_admin`); обработчики и диалоги спрашивают права через `UserService.is_admin`, ручное добавление и удаление, отмена после дедлайна, CSV | `services/admins.py`, `access.py`, `participants.py`, `export.py` |
| Пользователи и имена | Учёт нажавших, `dm_available`, регистрация inline-постов, проверка админа для UI; единые правила показа имён | `services/users.py`, `names.py` |
| Баны и явка | Автобан по порогу и автоснятие ниже порога, ручной бан, разбан, отметка и исправление явки только у завершённой встречи (окно 7 суток), истечение окна без бана, дожим пересчёта (`bans_evaluated_at`, задача `ban_recheck`) | `services/bans.py`, `attendance.py` |
| Явка: запросы | Запрос явки админам и напоминание в служебный чат через 14 часов | `services/attendance_prompts.py` |
| Напоминания | Участникам — за `reminder_hours_before` часов; админам — «пост не разослан» за `post_reminder_days_before` дней (один раз, с повтором не чаще раза в 30 минут при недоставке) | `services/reminders.py`, `post_reminders.py` |
| Жизненный цикл | Закрытие записи, завершение встречи (после начала и закрытия), автосоздание «через N дней после встречи» с защитой по `recurrence_key`, итог записи в служебный чат | `services/lifecycle.py` |
| Настройки и тексты | Схема раздела 7 (ключи, умолчания, диапазоны, валидация HTML и подстановок), аудит изменений, синхронный снимок (`*_now`) для сервисов, 19 текстов `text.*` | `services/settings.py`, `settings_schema.py`, `texts/defaults.py` |
| Тексты: рендер | Единые подстановки (`{date}`, `{time}`, `{end_time}`, `{weekday}`, `{place}`, `{deadline}`, `{position}`, `{name}`, `{reason}`), длины в UTF-16, служебные сообщения в чат админов | `texts/render.py`, `texts/service.py` |
| Ошибки сервисов | `ErrorCode` — единый перечень кодов `InvalidOperation`; тексты для админа — `telegram/errors_text.py` | `services/errors.py` |
| Порты Telegram | `Notifier`, `PostRefresher` и пустые реализации; реализации — `TelegramNotifier` и `PostUpdater` | `services/ports.py`, `telegram/` |
| Рендер поста | Чистые функции: текст (имена скрыты по умолчанию; «В очереди: N» при открытой записи; интервал «16:30–18:30»), клавиатура `r:<event_id>:j\|j1\|c`, хеш, лимиты 1024/4096; данные поста — `PostService`, `load_post_data` | `renderer/post.py`, `services/posts.py` |
| Telegram: посты | `PostUpdater`: debounce 1,5 с, повтор при `RetryAfter`, пометка `is_dead`, смена фото через `editMessageMedia` (`event_posts.has_photo`, `last_photo_file_id`) | `telegram/post_updater.py`, `posts.py`, `keyboards.py`, `callbacks.py` |
| Telegram: уведомления | `TelegramNotifier`: личка (при `Forbidden` сбрасывает `dm_available`), служебный чат с дублем владельцам при недоступности (дубль считается доставкой, повторов нет) и подсказкой при миграции группы, бэкап документом | `telegram/notifier.py` |
| Служебный чат | Проверка при запуске: публичная группа или не группа — предупреждение; метка «РЕЖИМ DEV» | `telegram/service_chat.py`, `app.py` |
| Обработчики | Кнопки записи (`signup`), inline-режим (`inline`), `/start` (+ deep links `remind`, `about`), `/help`, `/dev_shift`, членство бота в чатах (`start`), список явки (`attendance`), глобальные ошибки (`errors`), ответ «Не понял» (`fallback`) | `handlers/` |
| Middleware | `UserMiddleware` (актуализация данных пользователя), ограничитель частоты (5 событий за 2 секунды на человека) | `middlewares/` |
| Команды меню | `/start`, `/help` — всем; `/menu`, `/cancel` — только админам (по ролям) | `telegram/commands.py` |
| Админка (aiogram-dialog) | Главное меню, встречи (список, карточка, правка, моменты), мастер создания (русский календарь, значения прошлой встречи, предупреждение о длине), участники (пометка 📵), баны, админы, настройки, выбор пользователя; общие кнопки — `_buttons.py` | `dialogs/` |
| Отметка явки | Список ✅/🚫 в личке админа постранично (по 25), черновики под блокировкой сообщения, «Готово», режим исправления, погашение у остальных админов | `telegram/attendance_ui.py`, `handlers/attendance.py` |
| Планировщик | Тик раз в минуту, 10 задач в порядке: `auto_create`, `reminder`, `post_reminder`, `close_registration`, `finish_events`, `attendance_prompt`, `attendance_reminder`, `attendance_expire`, `ban_recheck`, `backup`; таймаут задачи 120 с; пульс в `data/heartbeat` | `scheduler/` |
| Бэкап | Ежедневно с 04:00 по времени из настроек: копия через SQLite backup API, проверка целостности, права 0600/0700, хранятся 14 копий, отправка документом в служебный чат | `scheduler/backup.py` |
| Корень сборки | `build_services(...)` связывает сервисы: один `EventLocks`, один снимок настроек, порядок задач | `container.py` |
| Запуск | `python -m meetup_bot` / `dev.py run`: миграции → бот → планировщик → long polling; при запуске догоняются посты встреч последних 14 дней (`POST_REFRESH_WINDOW`), при остановке дожидаются фоновые уведомления (`services.background.aclose()`); Sentry с вычисткой токена из событий | `app.py`, `__main__.py` |
| Контейнер | Многоэтапный `Dockerfile` (пользователь UID 10001, `APP_VERSION` при сборке), `docker/entrypoint.sh` (`umask 077`, проверка прав на `data`, миграции, запуск), `compose.yaml` (`read_only`, `cap_drop: ALL`, `no-new-privileges`, `tmpfs /tmp`, `mem_limit`, ротация логов, шаблон второй локации), healthcheck по файлу-пульсу | `Dockerfile`, `compose.yaml`, `docker/` |
| CI | `ci.yml`: `dev.py quality-gate` на PR и push в `main`. `deploy.yml` нет (M7) | `.github/workflows/` |
| Инструменты | `dev.py`, pre-commit, ruff (`DTZ`), mypy (строгий режим для `services/`, `repositories/`, `renderer/`, `telegram/`, `db/`, `clock`, `config`; тесты mypy не проверяет), pytest | корень, `pyproject.toml` |
| Система агентов | `.ai/`, `CLAUDE.md`, `AGENTS.md` (генерируется), `.claude/`, `.codex/` | корень |

Что не построено: автодеплой (`deploy.yml`), анонимизация данных по запросу, внешний мониторинг,
публикация постов от имени бота. Бот ни разу не запускался на реальном Telegram: это работа пилота.

## Тесты

`tests/`: 2261 тест (на Windows 3 пропускаются — проверки POSIX-прав файлов бэкапа; в CI на Linux
они идут). Тесты описывают требования: при расхождении меняется код, а не тест.

- `tests/services/` (≈1050) и `tests/scheduler/` (≈120) — бизнес-логика на настоящей файловой SQLite
  и `FakeClock`: запись и очередь, встречи, участники, баны, явка (только у завершённой встречи),
  админы, настройки и тексты, жизненный цикл, планировщик, бэкап, CSV, фоновые уведомления,
  ограничитель уведомлений. Покрыты все кейсы `docs/spec/16-testing.md`.
- `tests/telegram/` (≈330) — `PostUpdater` (в том числе смена фото), `TelegramNotifier`, обработчики
  кнопок, inline, `/start`, middleware, ограничитель частоты, проверка служебного чата, лимиты
  `callback_data` и ответов.
- `tests/dialogs/` (≈470) — меню админа и отметка явки через фейковый Telegram: мастер, карточка,
  участники, баны, настройки, лимиты рендера, слои.
- `tests/renderer/`, `tests/texts/` — пост (интервал, очередь, лимиты, экранирование), подстановки и
  тексты по умолчанию.
- Корень `tests/` — `test_architecture.py` (≈180 проверок: границы слоёв, время только из `Clock`,
  единственность определений, нет пользовательских текстов в сервисах, нет записи в БД из
  диалогов и обработчиков, коды ошибок только из `ErrorCode`), `test_alembic.py` (миграции на чистой
  базе и на базе с дочерними строками, откаты `0004`–`0006`), `test_infra.py` (ограничения контейнера, `ENV=prod` в `.env.example`,
  `entrypoint.sh`), `test_config.py`, `test_db.py`, `test_clock.py`, `test_sync_agents.py`,
  `test_tg_fake.py`.
- Инфраструктура: `tests/helpers.py` (приложение на фейковых часах с записывающими портами,
  `person`, `make_app`), `tests/tg_fake.py` (фейковая сессия бота, построители обновлений, `Harness`
  с настоящими роутерами и диалогами), фикстуры `app` и `tg` в `tests/conftest.py`.
- **Строгий фейк Telegram.** `FakeSession` отвечает как настоящий Bot API: отклоняет текст длиннее
  4096 (по видимой длине в UTF-16), подпись длиннее 1024, `callback_data` длиннее 64 байт, ответ на
  callback длиннее 200 символов; правка без клавиатуры снимает прежние кнопки; правка по
  `inline_message_id` возвращает `True`; нажатие на исчезнувшую кнопку падает с понятной ошибкой.
  Внедряемые сбои имеют приоритет над проверками.

Реальный Telegram тестами не охвачен (нужен тестовый бот): сценарии ручной проверки —
`docs/telegram-test-checklist.md`.

## Целевые слои

Описаны в `docs/spec/09-architecture.md` и `.ai/project/12-domain-rules.md`:
`handlers/dialogs → services → repositories → db`; `renderer`, `telegram`, `scheduler`.
