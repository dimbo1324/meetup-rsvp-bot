# Текущее состояние системы

Что **реально построено** прямо сейчас. Намерение и целевая архитектура — `docs/spec/`;
порядок работ — `docs/ROADMAP.md`. Если этот документ расходится с кодом, прав код: исправьте
документ в той же задаче.

_Обновлено: 2026-10-23 (бизнес-логика M3–M6, M9 и Telegram-слой M2 готовы без тестов)._

## Что есть

| Область | Состояние | Где |
|---|---|---|
| Конфигурация | Готово: `Settings` из окружения (`BOT_TOKEN`, `OWNER_IDS`, `SERVICE_CHAT_ID`, `DATABASE_URL`, `ENV`, `LOG_LEVEL`, `SENTRY_DSN`) | `src/meetup_bot/config.py` |
| Время | Готово: `Clock`, `SystemClock`, `FakeClock` | `src/meetup_bot/clock.py` |
| БД: основа | Готово: `Base` с naming convention, `UtcDateTime`, движок с PRAGMA (WAL, busy_timeout, foreign_keys, synchronous) | `src/meetup_bot/db/` |
| БД: модели и миграции | Готово: все 8 таблиц раздела 10 (users, admins, events, registrations, event_posts, bans, settings, audit_log), миграция `0001`, частичные уникальные индексы, CHECK-ограничения | `src/meetup_bot/db/models.py`, `alembic/versions/` |
| SQLite-транзакции | Готово: каждая транзакция начинается с `BEGIN IMMEDIATE` (драйвер в автокоммите) | `src/meetup_bot/db/session.py` |
| Время встречи | Готово: расчёт моментов 5.2, ближайшая дата, инварианты | `src/meetup_bot/services/timing.py` |
| Репозитории | Готово: events, registrations, users, bans, audit | `src/meetup_bot/repositories/` |
| Сервис встреч | Готово: создание (`create_event_as_admin` для админа, `create_event` — системный), правка, перенос, лимит, закрытие/открытие, отмена, моменты | `src/meetup_bot/services/events.py`, `event_validation.py` |
| Админы и участники | Готово: роли, ручное добавление/удаление, отмена после дедлайна, CSV | `services/admins.py`, `participants.py`, `export.py` |
| Баны и явка | Готово: автобан по порогу, разбан, отметка и исправление явки, истечение окна 7 суток | `services/bans.py`, `attendance.py` |
| Лист ожидания | Готово: `promote_waitlist` и вызовы при освобождении мест | `services/waitlist.py` |
| Настройки и тексты | Готово: схема раздела 7, валидация, аудит, синхронный снимок (`*_now`), тексты `text.*` | `services/settings.py`, `settings_schema.py`, `texts/defaults.py` |
| Планировщик | Готово: тик раз в минуту, 8 задач, бэкап, пульс; запускается из `app.run` | `services/lifecycle.py`, `scheduler/` |
| Порты Telegram | Определены в `services/ports.py`, обёртки `services/safe.py`; реализации: `TelegramNotifier`, `PostUpdater` | `src/meetup_bot/telegram/` |
| Обработчики | Готово: кнопки записи/отмены, inline, `/start`, `/help`, deep links, ошибки, `/dev_shift` (dev). Меню админа (`/menu`) — заглушка | `src/meetup_bot/handlers/`, `middlewares/` |
| Запуск | Готово: `python -m meetup_bot` / `dev.py run` (миграции → бот → планировщик → long polling) | `src/meetup_bot/app.py`, `__main__.py` |
| Корень сборки | Готово: `build_services(...)` связывает сервисы; вызывается из `app.run` | `src/meetup_bot/container.py` |
| Сервис записи | Готово: `register`, `cancel`, блокировка по `event_id`, результаты-значения, аудит, продвижение очереди | `src/meetup_bot/services/registration.py` |
| Рендер поста | Готово: текст, клавиатура (`r:<event_id>:j\|j1\|c`), хеш, лимиты 1024/4096 | `src/meetup_bot/renderer/post.py`, `services/posts.py` |
| Telegram-слой, inline, post_updater | Нет (M2) | — |
| Админка (aiogram-dialog) | Нет (интерфейсная часть M3) | — |
| Контейнер | Dockerfile и compose есть; healthcheck ждёт файл-пульс (появится в M4) | `Dockerfile`, `compose.yaml` |
| CI | `ci.yml`: `dev.py quality-gate` на PR и push в `main`. `deploy.yml` нет (M7) | `.github/workflows/` |
| Инструменты разработки | `dev.py`, pre-commit, ruff, mypy, pytest | корень |
| Система агентов | `.ai/`, `CLAUDE.md`, `AGENTS.md` (генерируется), `.claude/`, `.codex/` | корень |

## Тесты

`tests/`: часы, типы БД и PRAGMA, конфигурация, синхронизация `AGENTS.md`, Alembic, тайминг, запись и
отмена (в том числе гонка 30 параллельных записей на файловой SQLite), рендер поста. Из
`docs/spec/16-testing.md` покрыты кейсы 1–6 и 17–19. **Логика M3–M6 и M9 тестами не покрыта** (решение владельца: тесты — отдельным этапом); поведение проверялось аудитом, ревью и сквозным сценарием через `container.build_services`.

## Целевые слои

Описаны в `docs/spec/09-architecture.md` и `.ai/project/12-domain-rules.md`:
`handlers/dialogs → services → repositories → db`; `renderer`, `telegram`, `scheduler`.
