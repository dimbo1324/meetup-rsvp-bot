# Текущее состояние кода: registro-civil-bot

Что **реально построено** сейчас (а не что запланировано — это `docs/ROADMAP.md`; целевая
архитектура — `SPEC.md`, раздел 2.3). Обновляется при каждом изменении формы системы.

## На 2026-11-11 (после подготовки репозитория)

- Пакет `src/rcbot/` содержит только `__init__.py` с версией. Слоёв `domain`, `application`,
  `infrastructure`, `bot`, CLI и миграций ещё нет.
- Зависимости объявлены в `pyproject.toml` бота и закреплены в общем `uv.lock` монорепозитория:
  aiohttp, aiogram, aiogram-dialog, SQLAlchemy + aiosqlite, Alembic, Pydantic,
  pydantic-settings, tzdata, sentry-sdk, uvloop (только Linux), Playwright, keyring; для тестов —
  aioresponses (pytest, ruff, mypy и pip-audit — общие инструменты корня).
- Chromium ставится командой `uv run python dev.py setup` в `bots/registro-civil-bot/.browsers/`
  (`PLAYWRIGHT_BROWSERS_PATH` задаёт `dev.py` из `[tool.monorepo].env`).
- Тесты: `tests/test_stack.py` — импорт стека, версии не ниже ТЗ, WAL в aiosqlite, фальшивый
  сервер `aiohttp.test_utils.TestServer`, запуск Chromium на странице из строки (без сети);
  aioresponses помечен `xfail(strict=True)` — несовместим с aiohttp 3.14 (вопрос Q-M3).

## Отклонения от SPEC.md, принятые при подготовке

Записаны как решения в `docs/open-questions.md` (R1–R4): место бота в монорепозитории вместо
`Desktop\RegistroBot\`, обезличенная копия ТЗ, общий `.venv` монорепозитория, команды
проверки через `dev.py`.
