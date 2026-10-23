---
name: milestone-episode
description: Используйте для планирования и выполнения одной вехи docs/ROADMAP.md от начала до конца: ориентация, план, реализация, проверка, обновление статуса.
---

# Выполнение вехи плана работ

Одна веха `docs/ROADMAP.md` — одна задача. Не сливайте две вехи в одну и не нарушайте
порядок без решения владельца, записанного в `docs/open-questions.md`.

## 1. Ориентация (обязательно)

```powershell
git status --short --branch
git log -15 --date=iso-strict --pretty=format:"%h %cd %s"
```

Затем по порядку: `docs/ROADMAP.md` (первая веха без `**Статус.**` — ваша),
`docs/architecture/overview.md`, `task-checklist.md`, `docs/open-questions.md`. Если в
чек-листе остались `[ ]` с прошлой сессии — сначала разберитесь с ними.

## 2. План

Для крупной или незнакомой вехи делегируйте субагенту `meetup-milestone-planner`.
Определите границы, владельцев поведения по слоям, затрагиваемые контракты (БД, тексты,
настройки, `callback_data`), риски для инвариантов и критерии приёмки.
Заполните `task-checklist.md` пунктами `[ ]` и **закоммитьте до кода**.

## 3. Ветка

```powershell
git checkout main
git pull --ff-only origin main
git checkout -b feat/<веха>-краткое-описание
```

## 4. Реализация

Подключайте специалистов: `meetup-core`, `meetup-telegram`, `meetup-platform`,
`meetup-security`. Обязательные тест-кейсы вехи — из `docs/spec/16-testing.md`.

## 5. Проверка

```powershell
uv run python dev.py quality-gate
```

Перед финалом — навык `code-review` или субагент `meetup-quality-reviewer`.

## 6. Завершение

- Заполните чек-лист значками `+`/`-` честно.
- Добавьте строку `**Статус.**` под вехой в `docs/ROADMAP.md` и обновите таблицу §1.
- Обновите `docs/architecture/overview.md` и `README.md`, если изменение видно пользователю.
- Если веха вскрыла устаревшие правила — примените навык `rules-evolution`.
- Fast-forward слияние в `main` и пуш — только если владелец явно просил публикацию.
- Итоговый отчёт: что сделано, что проверено, что не сделано.
