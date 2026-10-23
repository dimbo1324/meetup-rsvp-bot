# meetup-rsvp-bot — рабочие заметки для Claude Code

Это точка входа для Claude Code — главного AI-ассистента проекта. Все правила лежат в общих
модулях `.ai/` — едином источнике истины для каждого ассистента в этом репозитории. Codex и
другие читают те же модули через генерируемый `AGENTS.md`; руками его не править (меняйте
модуль, затем `uv run python dev.py sync-agents`).

Более поздние модули перекрывают более ранние; явное указание владельца в текущем диалоге
главнее всего. Документацию и комментарии в коде пишем **по-русски**.

## Начало новой сессии

Сессии часто начинаются без контекста. Перед любой работой выполните **ритуал ориентации**
из `@.ai/project/13-progress-tracking.md`: git status и log → `docs/ROADMAP.md` (первая веха
без строки `**Статус.**` — следующая) → `docs/architecture/overview.md` → `task-checklist.md`
→ `docs/open-questions.md`.

Продуктовое намерение — в `docs/spec/README.md` (индекс спецификации).

**Рутинные задачи — через `dev.py`**, а не собранные вручную команды:
`uv run python dev.py list` печатает каталог. Подробнее и обязанность поддерживать команды
актуальными — в `@.ai/project/11-commands.md`.

**Ветки.** В `main` работает только владелец. Вы всегда создаёте рабочую ветку, после
зелёного `quality-gate` сливаете её в `main` fast-forward и пушите — только если владелец
явно попросил опубликовать (см. `@.ai/universal/01-workflow.md`).

Правила предназначены для развития: когда и как их менять — `@.ai/universal/08-rules-evolution.md`;
что менялось — `.ai/CHANGELOG.md`.

## Универсальные правила (переносимы на любой проект)

- @.ai/universal/01-workflow.md
- @.ai/universal/02-task-checklist.md
- @.ai/universal/03-scope-and-code-style.md
- @.ai/universal/04-architecture-boundaries.md
- @.ai/universal/05-security-and-secrets.md
- @.ai/universal/06-quality-and-testing.md
- @.ai/universal/07-multi-assistant.md
- @.ai/universal/08-rules-evolution.md
- @.ai/universal/09-time-and-timestamps.md

## Правила проекта (meetup-rsvp-bot)

- @.ai/project/10-project-map.md
- @.ai/project/11-commands.md
- @.ai/project/12-domain-rules.md
- @.ai/project/13-progress-tracking.md

## Рабочая область Claude Code

- `.claude/settings.json` — списки разрешённых и запрещённых команд.
- `.claude/agents/` — субагенты проекта (`meetup-*`); зеркало `.codex/agents/` один к одному.
- `.claude/skills/` — повторяемые рабочие процессы (`milestone-episode`, `code-review`,
  `ci-fix`, `project-maintenance`, `rules-evolution`); зеркало `.codex/skills/`.
