# Текущее состояние системы

Что **реально построено** прямо сейчас. Намерение и целевая архитектура — `docs/spec/`;
порядок работ — `docs/ROADMAP.md`. Если этот документ расходится с кодом, прав код: исправьте
документ в той же задаче.

_Обновлено: 2026-10-23 (веха M0 завершена)._

## Что есть

| Область | Состояние | Где |
|---|---|---|
| Конфигурация | Готово: `Settings` из окружения (`BOT_TOKEN`, `OWNER_IDS`, `SERVICE_CHAT_ID`, `DATABASE_URL`, `ENV`, `LOG_LEVEL`, `SENTRY_DSN`) | `src/meetup_bot/config.py` |
| Время | Готово: `Clock`, `SystemClock`, `FakeClock` | `src/meetup_bot/clock.py` |
| БД: основа | Готово: `Base` с naming convention, `UtcDateTime`, движок с PRAGMA (WAL, busy_timeout, foreign_keys, synchronous) | `src/meetup_bot/db/` |
| БД: модели и миграции | Нет (веха M1). Alembic настроен, `alembic/versions/` пуст | `alembic/` |
| Сервисы, репозитории, renderer | Нет (M1) | — |
| Telegram-слой, inline, post_updater | Нет (M2) | — |
| Админка (aiogram-dialog) | Нет (M3) | — |
| Планировщик | Нет (M4) | — |
| Точка входа | Заглушка: загружает настройки и пишет в лог, бот не запускается | `src/meetup_bot/__main__.py` |
| Контейнер | Dockerfile и compose есть; healthcheck ждёт файл-пульс (появится в M4) | `Dockerfile`, `compose.yaml` |
| CI | `ci.yml`: `dev.py quality-gate` на PR и push в `main`. `deploy.yml` нет (M7) | `.github/workflows/` |
| Инструменты разработки | `dev.py`, pre-commit, ruff, mypy, pytest | корень |
| Система агентов | `.ai/`, `CLAUDE.md`, `AGENTS.md` (генерируется), `.claude/`, `.codex/` | корень |

## Тесты

`tests/`: часы, типы БД и PRAGMA, конфигурация, синхронизация `AGENTS.md`. Бизнес-тесты
(`docs/spec/16-testing.md`, кейсы 1–20) появятся с вехами M1–M6.

## Целевые слои

Описаны в `docs/spec/09-architecture.md` и `.ai/project/12-domain-rules.md`:
`handlers/dialogs → services → repositories → db`; `renderer`, `telegram`, `scheduler`.
