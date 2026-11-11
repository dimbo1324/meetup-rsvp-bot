# Чат-боты: монорепозиторий

Несколько независимых чат-ботов в одном репозитории: у каждого свой код, тесты, документы и
способ развёртывания, а инструменты, правила AI-ассистентов, CI и `uv.lock` — общие.

| Бот | Что делает | Где работает |
|---|---|---|
| [meetup-rsvp-bot](bots/meetup-rsvp-bot/README.md) | Запись на еженедельные встречи русскоязычного сообщества в Монтевидео: места, поимённая запись, бан за неявку, управление из Telegram | Linux-сервер, Docker |
| [registro-civil-bot](bots/registro-civil-bot/README.md) | Следит за свободными датами записи в Registro Civil и мгновенно сообщает владельцу (Telegram, push, звонок); только читает календарь | Ноутбук владельца, Windows |

## Быстрый старт

Понадобятся [Git](https://git-scm.com/) и [uv](https://docs.astral.sh/uv/); Python 3.14 uv
поставит сам. Все команды — из корня репозитория:

```bash
uv run python dev.py setup          # зависимости всех ботов, .env из шаблонов, шаги ботов, pre-commit
uv run python dev.py doctor         # что установлено и чего не хватает
uv run python dev.py bots           # список ботов
uv run python dev.py quality-gate   # полная проверка: правила, раскладка, lint, mypy, тесты
```

Работа с одним ботом — ключ `--bot <бот>`: `dev.py test --bot registro-civil-bot`,
`dev.py run --bot meetup-rsvp-bot`. Полный список команд — `uv run python dev.py list`.

## Документация

- [docs/monorepo.md](docs/monorepo.md) — устройство репозитория, контракт каталога бота,
  принятые решения, как добавить бота.
- [CONTRIBUTING.md](CONTRIBUTING.md) — ветки, коммиты, порядок работы.
- [CHANGELOG.md](CHANGELOG.md) — изменения монорепозитория; у каждого бота свой `CHANGELOG.md`.
- Документы бота — в его каталоге: `README.md`, требования, `docs/ROADMAP.md`,
  `docs/architecture/`, `docs/open-questions.md`.

## Для AI-ассистентов

Правила лежат в [`.ai/`](.ai/README.md) (общие) и `bots/<бот>/.ai/` (бота). Точки входа —
[`CLAUDE.md`](CLAUDE.md) для Claude Code и [`AGENTS.md`](AGENTS.md) для Codex и остальных
(генерируется), плюс одноимённые файлы в каталоге каждого бота.
