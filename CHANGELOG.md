# Журнал изменений

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/). Изменения правил для
AI-ассистентов записываются отдельно в `.ai/CHANGELOG.md`.

## [Не выпущено]

### Добавлено

- Веха M1: модели БД и миграция `0001`, расчёт времени встречи, сервисы создания встречи, записи и отмены,
  рендер поста с контролем длины и экранированием.

- Каркас проекта (веха M0): `pyproject.toml` (Python 3.14, uv), `config`, `clock`, `db`, Alembic,
  Dockerfile, compose, CI, pre-commit, `dev.py`.
- Система правил для AI-ассистентов: `.ai/`, `CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/`.
- Документация: спецификация разбита на `docs/spec/`, добавлены `docs/ROADMAP.md`,
  `docs/open-questions.md`, `docs/architecture/`.
