# Конфигурация Claude Code

Проектная конфигурация Claude Code для монорепозитория чат-ботов. Claude — главный ассистент.

Долговечные правила лежат в общих модулях `.ai/` (универсальные + монорепозиторий) и в
`bots/<бот>/.ai/` (правила бота). `CLAUDE.md` корня подключает общие модули нативным
синтаксисом `@`; `CLAUDE.md` бота — его модули, и Claude Code подгружает его при работе в
каталоге бота. Codex и остальные читают те же модули через генерируемые `AGENTS.md`
(`uv run python dev.py sync-agents`; руками не править). Субагентам следует читать `AGENTS.md`
корня и бота.

## Файлы

- `settings.json` — разрешённые рутинные команды чтения и проверки и явный запрет
  разрушительных git- и Docker-операций.
- `agents/` — субагенты для целевого делегирования; зеркало `.codex/agents/` по именам файлов.
- `skills/` — повторяемые рабочие процессы; зеркало `.codex/skills/`.
- `settings.local.json` — личные переопределения, в git не попадают.

## Рекомендуемое делегирование

Основную работу делайте в главном потоке; субагента запускайте только для независимой
работы, не требующей полного контекста.

Общие для репозитория:

- `milestone-planner` — спланировать веху `ROADMAP.md` любого бота до кода.
- `platform` — Docker, compose, CI/CD, `dev.py`, деплой, автозапуск.
- `quality-reviewer` — ревью diff до слияния.
- `ci-triage` — разбор упавшей проверки.
- `repo-maintainer` — форматирование, синхронизация правил, раскладка, документы состояния.

`meetup-rsvp-bot`:

- `meetup-core` — БД, репозитории, сервисы, планировщик, миграции.
- `meetup-telegram` — handlers, inline-режим, post_updater, renderer, меню админа.
- `meetup-security` — права, ввод, секреты, персональные данные.

`registro-civil-bot`:

- `rcbot-core` — домен, детектор, инциденты, опрос, рассылка, БД, конфиг.
- `rcbot-integrations` — вход, источники данных, HTTP, каналы уведомлений, Telegram, Windows.
- `rcbot-privacy` — секреты, редактирование логов, утечки, разрешённые хосты.

## Навыки

`milestone-episode`, `code-review`, `ci-fix`, `project-maintenance`, `rules-evolution`, `new-bot`.

## Быстрые команды

```powershell
uv run python dev.py quality-gate
uv run python dev.py quality-gate --bot registro-civil-bot
uv run python dev.py format
uv run python dev.py doctor
uv run python dev.py sync-agents --check
```

## Ветки

В `main` работает только владелец. Ассистент всегда создаёт рабочую ветку; слияние в `main`
и пуш — только по явной просьбе владельца и при зелёном шлюзе.

## Эволюция правил

Протокол — `.ai/universal/08-rules-evolution.md`, каждое изменение — в `.ai/CHANGELOG.md`.
Никогда не ослабляйте правило, чтобы текущая задача прошла.
