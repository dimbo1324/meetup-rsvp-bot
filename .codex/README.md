# Конфигурация Codex

Проектная конфигурация Codex для монорепозитория чат-ботов. Главный ассистент проекта —
Claude Code; Codex — равноправный помощник, подчиняющийся тем же правилам.

Правила лежат в общих модулях `.ai/` и в `bots/<бот>/.ai/`. Codex читает их через
генерируемые `AGENTS.md`: корневой — общие правила, `bots/<бот>/AGENTS.md` — правила бота
(Codex склеивает файлы от корня до рабочего каталога). Claude Code получает те же правила
через `CLAUDE.md` корня и бота.

`AGENTS.md` **генерируется** и не правится руками. После изменения любого модуля `.ai/`:

```powershell
uv run python dev.py sync-agents
```

## Файлы

- `config.toml` — модель и параметры агентов уровня проекта. Секретов здесь нет; личные
  переопределения — в игнорируемом `config.local.toml`.
- `agents/` — агенты проекта; зеркало `.claude/agents/`: `<имя>.toml` ↔ `<имя>.md`, поле
  `name` совпадает с именем файла.
- `skills/` — повторяемые рабочие процессы; зеркало `.claude/skills/`.

## Агенты

Общие: `milestone-planner`, `platform`, `quality-reviewer`, `ci-triage`, `repo-maintainer`.
`meetup-rsvp-bot`: `meetup-core`, `meetup-telegram`, `meetup-security`.
`registro-civil-bot`: `rcbot-core`, `rcbot-integrations`, `rcbot-privacy`.

## Навыки

`milestone-episode`, `code-review`, `ci-fix`, `project-maintenance`, `rules-evolution`, `new-bot`.

## Зеркала

`.codex/agents|skills` и `.claude/agents|skills` совпадают по именам файлов. Правка одной
стороны требует эквивалентной правки другой в той же задаче; `dev.py quality-gate` проверяет
паритет.

## Ветки

В `main` работает только владелец. Ассистент создаёт рабочую ветку; слияние и пуш — только
по явной просьбе владельца.
