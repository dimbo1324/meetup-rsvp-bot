---
name: project-maintenance
description: Используйте для рутинного обслуживания монорепозитория: форматирование, синхронизация AGENTS.md, паритет зеркал, раскладка ботов, обновление документов состояния.
---

# Обслуживание репозитория

```powershell
uv run python dev.py format
uv run python dev.py sync-agents
uv run python dev.py doctor
uv run python dev.py quality-gate
```

## Чек-лист

- Корневой и вложенные `AGENTS.md` синхронизированы с `.ai/` (`sync-agents --check`).
- `.claude/agents|skills` и `.codex/agents|skills` совпадают по именам файлов (проверяет шлюз).
- `dev.py check-layout` зелёный; таблица ботов в `.ai/project/10-project-map.md` совпадает с
  `dev.py bots`.
- У каждого бота: статусы вех `docs/ROADMAP.md` соответствуют коду; `overview.md` описывает
  реальную форму системы; `open-questions.md` содержит актуальные решения; `README.md`
  соответствует тому, как бот действительно запускается.
- `uv.lock` синхронизирован с `pyproject.toml` корня и ботов (`uv lock --check`).
- В `task-checklist.md` нет брошенных `[ ]`.

Дрейф «код ↔ документы»: код — факт, документ — намерение; согласуйте в этой же задаче.
Делегировать можно субагенту `repo-maintainer`.
