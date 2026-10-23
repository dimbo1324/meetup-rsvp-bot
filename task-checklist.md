# Чек-лист задачи: M2 — Telegram-слой (посты, кнопки, inline, уведомления)

Ветка: `feat/m2-telegram-layer`. Тесты и меню админа (aiogram-dialog) вне объёма.
Читать: `docs/spec/08-telegram-api.md`, `05-business-logic.md` (5.3, 5.4, 5.10), `09-architecture.md` (9.4, 9.5), `06-message-texts.md`.

## Подготовка

- [x] Ритуал ориентации; рабочая ветка от актуального `main`
- [ ] Контракты: `repositories/event_posts`, `users.set_dm_available`, `ShiftableClock`

## Реализация

- [ ] `telegram/`: `post_updater` (debounce 1,5 с, правка по `inline_message_id`, ретраи, `is_dead`), `notifier` (порты `Notifier`), `callbacks`, преобразование клавиатуры
- [ ] `handlers/`: `signup` (кнопки, lazy registration, всплывающие тексты, подсказка про Start), `inline` (`inline_query`, `chosen_inline_result`), `start` (`/start`, `/help`, deep links, `dm_available`, `my_chat_member`), `errors`
- [ ] `middlewares/`: пользователь (upsert), контекст сервисов
- [ ] Сборка: `__main__`, приложение (Bot, Dispatcher, `allowed_updates`, команды, стартовое сообщение, планировщик, остановка), dev-команда `/dev_shift`

## Проверка

- [ ] `quality-gate` зелёный (ruff, mypy `--strict`, 51 тест)
- [ ] Прогон на фейковом Telegram (подмена сессии бота): запись, повторное нажатие, мест нет, отмена, inline, пересборка постов
- [ ] Ревью diff (права, лимиты Telegram, экранирование, ошибки)

## Завершение

- [ ] Документы (`ROADMAP.md`, `overview.md`, `CHANGELOG.md`), чек-лист `+`/`-`
- [ ] Слияние fast-forward в `main`, пуш
