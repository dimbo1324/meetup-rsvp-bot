# Конфигурация Claude Code

Проектная конфигурация Claude Code для meetup-rsvp-bot. Claude — главный ассистент проекта.

Долговечные правила лежат в общих модулях `.ai/` (универсальные + проектные). `CLAUDE.md`
подключает их нативным синтаксисом `@`. Codex и остальные читают те же модули через
генерируемый `AGENTS.md` (`uv run python dev.py sync-agents`; руками не править). Субагентам
следует читать `AGENTS.md` — это собранный единый набор правил.

## Файлы

- `settings.json` — разрешённые рутинные команды чтения и проверки и явный запрет
  разрушительных git- и Docker-операций.
- `agents/` — субагенты проекта для целевого делегирования; зеркало `.codex/agents/`
  по именам.
- `skills/` — повторяемые рабочие процессы; зеркало `.codex/skills/`.
- `settings.local.json` — личные переопределения, в git не попадают.

## Рекомендуемое делегирование

Основную работу делайте в главном потоке; субагента запускайте только для независимой
работы, не требующей полного контекста:

- `meetup-milestone-planner` — спланировать веху `docs/ROADMAP.md` до кода.
- `meetup-core` — БД, репозитории, сервисы, планировщик, миграции.
- `meetup-telegram` — handlers, inline-режим, post_updater, renderer, меню админа.
- `meetup-platform` — Docker, compose, CI/CD, деплой, бэкапы.
- `meetup-security` — права, ввод, секреты, персональные данные.
- `meetup-quality-reviewer` — ревью diff до слияния.
- `meetup-ci-triage` — разбор упавшей проверки.
- `meetup-repo-maintainer` — форматирование, синхронизация правил, документы состояния.

## Навыки

`milestone-episode`, `code-review`, `ci-fix`, `project-maintenance`, `rules-evolution`.

## Быстрые команды

```powershell
uv run python dev.py quality-gate
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
