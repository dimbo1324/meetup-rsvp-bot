---
name: project-maintenance
description: Используйте для рутинного обслуживания: форматирование, синхронизация AGENTS.md, паритет зеркал, обновление документов состояния.
---

# Обслуживание репозитория

```powershell
uv run python dev.py format
uv run python dev.py sync-agents
uv run python dev.py doctor
uv run python dev.py quality-gate
```

## Чек-лист

- `AGENTS.md` синхронизирован с `.ai/` (`sync-agents --check`).
- `.claude/agents|skills` и `.codex/agents|skills` совпадают по именам (проверяет шлюз).
- `docs/ROADMAP.md`: статусы вех соответствуют коду; `docs/architecture/overview.md`
  описывает реальную форму системы; `docs/open-questions.md` содержит актуальные решения.
- `README.md` соответствует тому, как проект действительно запускается.
- `uv.lock` синхронизирован с `pyproject.toml` (`uv lock --check`).
- В `task-checklist.md` нет брошенных `[ ]`.

Дрейф «код ↔ документы»: код — факт, документ — намерение; согласуйте в этой же задаче.
Делегировать можно субагенту `meetup-repo-maintainer`.
