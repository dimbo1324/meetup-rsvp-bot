# 13. Стек и версии

Версии актуальны на 23 октября 2026 года. Совместимость с Python 3.14 и наличие готовых сборок для **x86_64 и aarch64** проверены через PyPI: весь набор зависимостей ставится без компиляции.

| Назначение | Пакет | Версия |
|---|---|---|
| Язык | CPython | **3.14** (обычная сборка, не free-threaded) |
| Менеджер проекта | uv | 0.13 |
| Telegram | aiogram | 3.31 |
| Меню админа | aiogram-dialog | 2.6 |
| ORM | SQLAlchemy (`[asyncio]`) | 2.1 |
| Драйвер SQLite | aiosqlite | 0.22 |
| Миграции | Alembic | 1.20 |
| Конфиг | pydantic-settings | 2.16 |
| Валидация | pydantic | 2.13 (подтягивается aiogram, см. ниже) |
| Часовые пояса | zoneinfo (stdlib) + tzdata | 2026.5 |
| Цикл событий | uvloop | 0.23 (опционально) |
| Логи | structlog | 26.1 (или stdlib `logging`) |
| Ошибки | sentry-sdk | 2.73 (опционально) |
| Тесты | pytest, pytest-asyncio | 9.1, 1.4 |
| Линтер и форматтер | ruff | 0.17 |
| Типы | mypy или pyright | последняя |
| Контейнер | Docker, образ `python:3.14-slim` | — |
| CI/CD | GitHub Actions, GHCR | — |
| Хостинг | Oracle Cloud Always Free | — |

**Важно:**

- **Не переходить на Python 3.15.** Он вышел в октябре 2026, но aiogram 3.31 требует Python `<3.15`. Образ всегда указывать как `python:3.14-slim`, а не `python:3-slim`.
- **Не фиксировать pydantic 2.14 вручную.** aiogram 3.31 требует `pydantic<2.14` и `aiohttp<3.15`. uv сам выберет совместимые версии (pydantic 2.13.5).
- Точные версии всех пакетов фиксируются в `uv.lock`, который коммитится в репозиторий. Обновлять зависимости — отдельным PR.

Пример `pyproject.toml`:

```toml
[project]
name = "meetup-rsvp-bot"
version = "0.1.0"
requires-python = ">=3.14,<3.15"
dependencies = [
    "aiogram>=3.31,<4",
    "aiogram-dialog>=2.6,<3",
    "sqlalchemy[asyncio]>=2.1,<2.2",
    "aiosqlite>=0.22",
    "alembic>=1.20",
    "pydantic-settings>=2.16",
    "tzdata>=2026.5",
    "uvloop>=0.23; sys_platform == 'linux'",
    "structlog>=26.1",
    "sentry-sdk>=2.73",
]

[dependency-groups]
dev = ["pytest>=9.1", "pytest-asyncio>=1.4", "ruff>=0.17", "mypy", "pre-commit"]

[tool.ruff]
line-length = 100
target-version = "py314"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "ASYNC", "RUF", "DTZ"]   # DTZ ловит naive datetime

[tool.pytest.ini_options]
asyncio_mode = "auto"
```
