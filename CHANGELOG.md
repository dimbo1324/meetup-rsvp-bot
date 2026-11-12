# Журнал изменений монорепозитория

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/). Здесь — изменения общего:
раскладки, инструментов, CI, общих зависимостей. У каждого бота свой `CHANGELOG.md`;
изменения правил для AI-ассистентов — в `.ai/CHANGELOG.md`.

## [Не выпущено]

### Изменено

- `.gitattributes`: файлы `.cmd` и `.bat` хранятся с CRLF (остальные текстовые — LF).
- `dev.py test`: полный прогон бота проверяет порог покрытия из `[tool.monorepo]`
  (`coverage` — пакеты, `coverage_min` — процент, необязательные ключи); `resolved_env` не
  перекрывает переменные, уже заданные в окружении запуска.
- CI: Windows-задание ставит Chromium для тестов бота.

### Исправлено

- `dev.py format`: `ruff format` выполняется и тогда, когда `ruff check --fix` оставил ошибки
  (раньше форматирование молча пропускалось).

### Добавлено

- **Монорепозиторий ботов (2026-11-11).** Корень — виртуальный корень uv workspace
  (`bots/*`, `packages/*`) с общими инструментами разработки (pytest, ruff, mypy, pre-commit,
  pip-audit) и одним `uv.lock`. `meetup-rsvp-bot` перенесён в `bots/meetup-rsvp-bot/` с
  сохранением истории; добавлен каталог `bots/registro-civil-bot/` с зависимостями из его ТЗ
  (aiohttp, Playwright, keyring и др.).
- `dev.py`: обнаружение ботов, ключ `--bot`, команды `bots`, `check-layout`, `audit`; проверки
  mypy и pytest по каждому боту отдельным процессом.
- `scripts/monorepo.py`: контракт каталога бота и запрет импорта одного бота из другого — в
  шлюзе качества и pre-commit.
- `scripts/sync_agents.py`: вложенные `AGENTS.md` ботов, бюджет на пару «корень + бот».
- CI: задание на Windows для ботов с платформой `windows-desktop`, сборка Docker-образов,
  отмена устаревших прогонов; dependabot для `uv.lock` и Docker-образов ботов.
- `docs/monorepo.md` — устройство и решения; `packages/` — место для общих библиотек.
