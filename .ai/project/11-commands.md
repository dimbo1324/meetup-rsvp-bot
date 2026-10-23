# Команды проекта

Рутинные задачи выполняются через `dev.py` (только стандартная библиотека, кроссплатформенно),
а не через собранные вручную последовательности. Каталог: `uv run python dev.py list`.

| Команда | Что делает |
|---|---|
| `doctor` | Проверяет окружение: Python 3.14, uv, git, `.env`, зависимости, хуки |
| `setup` | `uv sync`, копия `.env.example` → `.env` (если нет), установка pre-commit-хуков |
| `sync-agents` | Перегенерирует `AGENTS.md` из `.ai/`; `--check` только проверяет синхронизацию |
| `format` | `ruff format` + `ruff check --fix` |
| `lint` | `ruff check` + `ruff format --check` |
| `typecheck` | `mypy` (строгий режим для `services/`, `repositories/`, `renderer/`) |
| `test` | `pytest`, аргументы передаются дальше |
| `quality-gate` | Полный шлюз: `sync-agents --check`, lint, typecheck, test. Обязателен перед слиянием |
| `migrate` | `alembic upgrade head` |
| `run` | Запуск бота: `python -m meetup_bot` |

Запуск: `uv run python dev.py <команда>`. Прямые вызовы `uv run ruff|mypy|pytest` тоже
допустимы, но шлюз качества запускается только через `dev.py quality-gate`.

## Обязанность держать команды актуальными

Если задача изменила способ сборки, проверки или запуска проекта, обновите `dev.py` и эту
таблицу в той же задаче. Если новая подсистема (например, Docker-проверки) стала частью
процесса — добавьте её в `quality-gate`.

## Среда разработки

- Python 3.14 (`.python-version`), менеджер — `uv`; точные версии — `uv.lock` (коммитится).
- Не переходить на Python 3.15: aiogram 3.31 требует `<3.15` (см. `docs/spec/13-stack.md`).
- `.env` создаётся из `.env.example` и не коммитится.
