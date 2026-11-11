---
name: new-bot
description: Используйте, чтобы добавить в монорепозиторий нового бота: каталог по контракту, зависимости, документы, правила и агенты, проверка шлюзом.
---

# Добавление нового бота

Контракт каталога и решения — `docs/monorepo.md`; правила — `.ai/project/12-monorepo.md`.
Образец — существующие боты в `bots/`. Код фаз нового бота в этой задаче не пишется.

## 1. Имя и место

- Каталог `bots/<имя>-bot/` (латиница, дефисы), дистрибутив с тем же именем, пакет — короткое
  имя в `snake_case`, не совпадающее с пакетами других ботов.
- Платформа: `linux-docker` (сервер) или `windows-desktop` (машина владельца).

## 2. Каталог бота

- `pyproject.toml`: `[project]` с зависимостями бота (обоснование — `05-security-and-secrets.md`),
  dev-группа только для зависимостей тестов этого бота, `[tool.monorepo]` (`package`,
  `platform`, при необходимости `env` и `setup`), `[tool.ruff] extend = "../../pyproject.toml"`,
  свои `[tool.mypy]`, `[tool.pytest.ini_options]`.
- `src/<пакет>/__init__.py`, `tests/__init__.py` и хотя бы один тест.
- `README.md`, `CHANGELOG.md`, `.env.example` (без секретов), требования (`docs/spec/` или
  `SPEC.md`; персональные данные владельца — метками, реальные значения — в `.env`).
- `docs/ROADMAP.md`, `docs/architecture/overview.md`, `docs/architecture/invariants.md`,
  `docs/open-questions.md`.
- `CLAUDE.md` бота (импортирует модули `.ai/`), модули `.ai/*.md` с доменными правилами.
- Для `linux-docker`: `Dockerfile` с контекстом — корнем монорепозитория,
  `Dockerfile.dockerignore` (разрешающий список), `compose.yaml` с `context: ../..`.

## 3. Корень

- `uv lock`, затем `uv run python dev.py setup --bot <бот>`.
- `uv run python dev.py sync-agents` — появится `bots/<бот>/AGENTS.md`.
- Строка в таблице ботов `.ai/project/10-project-map.md` и `docs/monorepo.md`; запись в
  `.ai/CHANGELOG.md`; корневой `README.md`.
- При необходимости агенты `<префикс>-*` — в `.claude/agents/` и `.codex/agents/` одновременно.
- CI: `windows-desktop` — в матрицу Windows-задания; Docker — в `.github/dependabot.yml`.

## 4. Проверка

```powershell
uv run python dev.py check-layout
uv run python dev.py quality-gate
```
