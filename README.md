# meetup-rsvp-bot

Telegram-бот для записи на субботние встречи русскоязычного сообщества в Монтевидео:
ограниченное число мест (по умолчанию 10), поимённая запись, бан за неявку, управление
из Telegram без правки кода.

> **Статус:** идёт разработка, готов каркас (веха M0). Прогресс — [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Быстрый старт

Нужны [Git](https://git-scm.com/) и [uv](https://docs.astral.sh/uv/) (Python 3.14 uv поставит сам).

```bash
git clone https://github.com/dimbo1324/meetup-rsvp-bot.git
cd meetup-rsvp-bot
uv run python dev.py setup      # зависимости, .env из шаблона, pre-commit-хуки
```

Впишите в `.env` **токен своего тестового бота** (один токен — один процесс long polling,
у каждого разработчика свой бот) и свой Telegram ID, затем:

```bash
uv run python dev.py migrate    # применить миграции
uv run python dev.py run        # запустить бота
```

## Рабочие команды

Все рутинные задачи — через `dev.py` (`uv run python dev.py list`):

| Команда | Назначение |
|---|---|
| `doctor` | проверить окружение |
| `quality-gate` | полный шлюз качества: синхронизация правил, lint, mypy, pytest |
| `format` / `lint` / `typecheck` / `test` | отдельные проверки |
| `sync-agents` | перегенерировать `AGENTS.md` из `.ai/` |
| `migrate` | `alembic upgrade head` |

## Документация

- [`docs/spec/`](docs/spec/README.md) — описание продукта по разделам (бизнес-логика, Telegram API,
  архитектура, модель данных, деплой и т. д.)
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — вехи и статус
- [`docs/open-questions.md`](docs/open-questions.md) — решения владельца и открытые вопросы
- [`docs/architecture/overview.md`](docs/architecture/overview.md) — что построено сейчас
- [`docs/architecture/invariants.md`](docs/architecture/invariants.md) — что нельзя ломать
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — как работать с репозиторием

## Для AI-ассистентов

Репозиторий настроен для совместной работы нескольких AI-ассистентов: правила лежат в
[`.ai/`](.ai/README.md), точки входа — [`CLAUDE.md`](CLAUDE.md) (Claude Code, главный ассистент)
и [`AGENTS.md`](AGENTS.md) (Codex и остальные; генерируется). Любой ассистент восстанавливает
состояние проекта из файлов по «ритуалу ориентации» из `.ai/project/13-progress-tracking.md`.

## Стек

Python 3.14 · aiogram 3 · aiogram-dialog · SQLAlchemy 2 (async) · SQLite · Alembic ·
pydantic-settings · uv · ruff · mypy · pytest · Docker · GitHub Actions.

## Язык

Вся документация и комментарии в коде — на русском: проект делается для русскоязычного сообщества.
