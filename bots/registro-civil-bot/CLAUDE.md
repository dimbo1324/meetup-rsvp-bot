# registro-civil-bot (rcbot) — правила бота для Claude Code

Claude Code загружает этот файл, когда работа касается каталога `bots/registro-civil-bot/`.
Он **дополняет** корневой `CLAUDE.md` монорепозитория: общие правила действуют, правила бота
главнее там, где они строже. Codex читает те же правила через сгенерированный `AGENTS.md`
этого каталога (руками не править: меняйте модуль `.ai/`, затем `dev.py sync-agents`).

Требования — `SPEC.md` в этом каталоге. План — `docs/ROADMAP.md`; инварианты —
`docs/architecture/invariants.md`; решения по размещению в монорепозитории — `docs/open-questions.md`.

@.ai/10-bot-rules.md
