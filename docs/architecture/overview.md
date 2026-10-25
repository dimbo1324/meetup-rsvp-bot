# Текущее состояние системы

Что **реально построено** прямо сейчас. Намерение и целевая архитектура — `docs/spec/`;
порядок работ — `docs/ROADMAP.md`. Если этот документ расходится с кодом, прав код: исправьте
документ в той же задаче.

_Обновлено: 2026-10-25 (веха M6.1: умолчания и бизнес-логика по ТЗ заказчика; меню и мастер — следующий этап)._

## Что есть

| Область | Состояние | Где |
|---|---|---|
| Конфигурация | Готово: `Settings` из окружения (`BOT_TOKEN`, `OWNER_IDS`, `SERVICE_CHAT_ID`, `DATABASE_URL`, `ENV`, `LOG_LEVEL`, `SENTRY_DSN`) | `src/meetup_bot/config.py` |
| Время | Готово: `Clock`, `SystemClock`, `FakeClock` | `src/meetup_bot/clock.py` |
| БД: основа | Готово: `Base` с naming convention, `UtcDateTime`, движок с PRAGMA (WAL, busy_timeout, foreign_keys, synchronous) | `src/meetup_bot/db/` |
| БД: модели и миграции | Готово: все 8 таблиц раздела 10 (users, admins, events, registrations, event_posts, bans, settings, audit_log), миграции `0001`–`0004`, частичные уникальные индексы, CHECK-ограничения | `src/meetup_bot/db/models.py`, `alembic/versions/` |
| SQLite-транзакции | Готово: каждая транзакция начинается с `BEGIN IMMEDIATE` (драйвер в автокоммите) | `src/meetup_bot/db/session.py` |
| Время встречи | Готово: расчёт моментов 5.2, ближайшая дата, инварианты | `src/meetup_bot/services/timing.py` |
| Репозитории | Готово: events, registrations, users, bans, audit | `src/meetup_bot/repositories/` |
| Сервис встреч | Готово: создание (`create_event_as_admin` для админа, `create_event` — системный), правка, перенос, лимит, закрытие/открытие, отмена, моменты | `src/meetup_bot/services/events.py`, `event_validation.py` |
| Админы и участники | Готово: роли, проверка прав (`require_admin`/`require_owner`), ручное добавление/удаление, отмена после дедлайна, CSV | `services/admins.py`, `access.py`, `participants.py`, `export.py` |
| Пользователи | Готово: учёт нажавших, `dm_available`, регистрация inline-постов, отображаемое имя | `services/users.py`, `names.py` |
| Баны и явка | Готово: автобан по порогу и автоснятие ниже порога, разбан, отметка и исправление явки, истечение окна 7 суток, дожим пересчёта (`bans_evaluated_at`, задача `ban_recheck`) | `services/bans.py`, `attendance.py` |
| Лист ожидания | Готово: `promote_waitlist` и вызовы при освобождении мест | `services/waitlist.py` |
| Настройки и тексты | Готово: схема раздела 7 (умолчания: закрытие записи 0 ч, имена скрыты, очередь включена), валидация, аудит, синхронный снимок (`*_now`), тексты `text.*`; единый рендер подстановок и длины в UTF-16 | `services/settings.py`, `settings_schema.py`, `texts/defaults.py`, `texts/render.py` |
| Планировщик | Готово: тик раз в минуту, 9 задач (включая `ban_recheck`; автосоздание «через N дней после встречи»), бэкап, пульс; запускается из `app.run` | `services/lifecycle.py`, `scheduler/` |
| Порты Telegram | Определены в `services/ports.py`, обёртки `services/safe.py`; реализации: `TelegramNotifier`, `PostUpdater` | `src/meetup_bot/telegram/` |
| Обработчики | Готово: кнопки записи/отмены, inline, `/start`, `/help`, deep links, ошибки, `/dev_shift` (dev). `/menu` открывает меню админа (диалоги) | `src/meetup_bot/handlers/`, `middlewares/` |
| Запуск | Готово: `python -m meetup_bot` / `dev.py run` (миграции → бот → планировщик → long polling) | `src/meetup_bot/app.py`, `__main__.py` |
| Корень сборки | Готово: `build_services(...)` связывает сервисы; вызывается из `app.run` | `src/meetup_bot/container.py` |
| Сервис записи | Готово: `register`, `cancel`, блокировка по `event_id`, результаты-значения, аудит, продвижение очереди | `src/meetup_bot/services/registration.py` |
| Рендер поста | Готово: текст (имена скрыты по умолчанию; «В очереди: N» при открытой записи), клавиатура (`r:<event_id>:j\|j1\|c`), хеш, лимиты 1024/4096 | `src/meetup_bot/renderer/post.py`, `services/posts.py` |
| Команды меню | Готово: `/start`, `/help` всем; `/menu`, `/cancel` — только админам (по ролям) | `src/meetup_bot/telegram/commands.py` |
| Админка (aiogram-dialog) | Готово: главное меню, встречи, мастер, участники, баны, админы, настройки, выбор пользователя | `src/meetup_bot/dialogs/` |
| Отметка явки | Готово: список ✅/🚫 в личке админа постранично (по 25), черновики под блокировкой сообщения, «Готово», режим исправления, погашение у остальных | `telegram/attendance_ui.py`, `handlers/attendance.py` |
| Контейнер | Dockerfile и compose есть; healthcheck по файлу-пульсу планировщика | `Dockerfile`, `compose.yaml` |
| CI | `ci.yml`: `dev.py quality-gate` на PR и push в `main`. `deploy.yml` нет (M7) | `.github/workflows/` |
| Инструменты разработки | `dev.py`, pre-commit, ruff, mypy, pytest | корень |
| Система агентов | `.ai/`, `CLAUDE.md`, `AGENTS.md` (генерируется), `.claude/`, `.codex/` | корень |

## Тесты

`tests/`: 1672 теста (2026-10-24; 2 пропускаются на Windows — права файлов бэкапа). Общая инфраструктура: `tests/helpers.py` (приложение на фейковых часах
с записывающими портами), `tests/tg_fake.py` (фейковая сессия бота, построители обновлений, `Harness` с
диспетчером и диалогами), фикстуры `app` и `tg`.
- `tests/services/`, `tests/scheduler/` — бизнес-логика: запись и очередь, встречи, участники, баны, явка,
  админы, настройки и тексты, жизненный цикл, планировщик, бэкап, CSV. Покрыты все кейсы `docs/spec/16-testing.md`
  (1–20); покрытие сервисов и планировщика 97–100%.
- `tests/telegram/` — `PostUpdater`, `TelegramNotifier`, обработчики кнопок, inline, `/start`, ошибки, middleware.
- `tests/dialogs/` — меню админа и интерфейс отметки явки через фейковый Telegram; покрытие диалогов 80–100%.
- `tests/test_architecture.py` — границы слоёв, время только из `Clock`, единственность определений, нет
  текстов в сервисах и записи в БД из диалогов/обработчиков.
Реальный Telegram тестами не охвачен (нужен тестовый бот).

## Целевые слои

Описаны в `docs/spec/09-architecture.md` и `.ai/project/12-domain-rules.md`:
`handlers/dialogs → services → repositories → db`; `renderer`, `telegram`, `scheduler`.
