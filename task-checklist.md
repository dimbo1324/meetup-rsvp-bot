# Чек-лист задачи: M1 — ядро записи

Ветка: `feat/m1-registration-core`

Читать: `docs/spec/05-business-logic.md` (5.1–5.4, 5.10), `10-data-model.md`, `09-architecture.md` (9.3), `16-testing.md`.

## Подготовка

- [x] Ритуал ориентации выполнен (git log, ROADMAP, overview, open-questions: блокирующих вопросов нет, действуют значения по умолчанию)
- [x] Рабочая ветка создана от актуального `main`

## Реализация

- [ ] PRAGMA + `BEGIN IMMEDIATE` для SQLite в `db/session.py`
- [ ] Модели всех таблиц раздела 10 (`db/models.py`) и первая миграция Alembic
- [ ] Расчёт моментов времени 5.2 с инвариантами (`services/timing.py`), учёт часового пояса `America/Montevideo`
- [ ] Репозитории: events, registrations, users, bans, audit
- [ ] `services/events.py`: создание встречи с вычисленными моментами
- [ ] `services/registration.py`: `register` (5.3) и `cancel` (5.4), блокировка по `event_id`, значения-результаты
- [ ] `renderer/post.py`: текст, клавиатура, хеш, экранирование, контроль длины 1024/4096

## Проверка (тест-кейсы `docs/spec/16-testing.md`)

- [ ] 1 — 11-я запись получает `FULL`
- [ ] 2 — 30 параллельных `register()` на 10 мест → ровно 10 `OK`
- [ ] 3 — двойное нажатие → `ALREADY`
- [ ] 4 — отмена за минуту до и через минуту после дедлайна → `OK` / `TOO_LATE`
- [ ] 5 — забаненный → `BANNED`
- [ ] 6 — запись после `registration_closes_at` → `CLOSED`
- [ ] 17 — суббота 17:00 в Монтевидео = 20:00 UTC
- [ ] 18 — имя с `<b>` и `&` экранируется
- [ ] 19 — пост с фото и 10 длинными именами: подпись ≤ 1024, хвост «… и ещё N»
- [ ] `dev.py quality-gate` зелёный; `mypy --strict` для services/repositories/renderer/db; покрытие сервисов ≥ 85%
- [ ] Миграция применяется к чистой базе и совпадает с моделями

## Завершение

- [ ] Документы: `ROADMAP.md` (**Статус.** M1), `overview.md`, `CHANGELOG.md`
- [ ] Ревью diff (`code-review`), чек-лист заполнен `+`/`-`
- [ ] Коммит, слияние fast-forward в `main`, пуш
