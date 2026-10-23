# Конфигурация Codex

Проектная конфигурация Codex для meetup-rsvp-bot. Главный ассистент проекта — Claude Code;
Codex — равноправный помощник, подчиняющийся тем же правилам.

Правила лежат в общих модулях `.ai/`. Codex читает их через генерируемый `AGENTS.md` в корне —
собранный единый набор правил. Claude Code получает те же правила через `CLAUDE.md`.

`AGENTS.md` **генерируется** и не правится руками. После изменения любого модуля `.ai/`:

```powershell
uv run python dev.py sync-agents
```

## Файлы

- `config.toml` — модель и параметры агентов уровня проекта. Секретов здесь нет; личные
  переопределения — в игнорируемом `config.local.toml`.
- `agents/` — агенты проекта; зеркало `.claude/agents/` по именам (`<имя>.toml` ↔
  `meetup-<имя>.md`).
- `skills/` — повторяемые рабочие процессы; зеркало `.claude/skills/`.

## Агенты

`milestone-planner`, `core`, `telegram`, `platform`, `security`, `quality-reviewer`,
`ci-triage`, `repo-maintainer` (в Codex имена приведены к `meetup-<имя>`).

## Навыки

`milestone-episode`, `code-review`, `ci-fix`, `project-maintenance`, `rules-evolution`.

## Зеркала

`.codex/agents|skills` и `.claude/agents|skills` совпадают по именам. Правка одной стороны
требует эквивалентной правки другой в той же задаче; `dev.py quality-gate` проверяет паритет.

## Ветки

В `main` работает только владелец. Ассистент создаёт рабочую ветку; слияние и пуш — только
по явной просьбе владельца.
