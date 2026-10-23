# Чек-лист задачи: бизнес-логика вех M3–M6 и M9 (без тестов и Telegram-слоя)

Ветка: `feat/business-logic-m3-m9`. Модель работы: лид-оркестратор + 5 агентов в отдельных worktree.
Тесты и Telegram-интерфейс (aiogram, dialogs, handlers, post_updater) вне объёма — по решению владельца.

## Подготовка

- [+] Общие контракты: `errors`, `locks`, `ports`, `rules`, заглушка `waitlist`

## Реализация (по агентам)

- [ ] A. Настройки и тексты (M6): `services/settings.py`, `texts/defaults.py`, `repositories/settings.py`
- [ ] B. Админы, встречи, участники, CSV (M3): `services/admins.py`, `events.py`, `participants.py`, `export.py`
- [ ] C. Баны и явка (M5): `services/bans.py`, `services/attendance.py`
- [ ] D. Лист ожидания (M9): `services/waitlist.py`, правки `services/registration.py`
- [ ] E. Планировщик (M4): `services/lifecycle.py`, `scheduler/` (тик, задачи, бэкап, пульс)

## Проверка

- [ ] Слияние веток агентов без конфликтов
- [ ] `ruff`, `mypy --strict`, существующие 51 тест зелёные
- [ ] Аудит соответствия спецификации (разделы 5, 7, 10, 11)
- [ ] Код-ревью и ревью безопасности; находки исправлены

## Завершение

- [ ] `ROADMAP.md`, `overview.md`, `CHANGELOG.md`, `invariants.md` обновлены
- [ ] Чек-лист заполнен, слияние fast-forward в `main`, пуш
