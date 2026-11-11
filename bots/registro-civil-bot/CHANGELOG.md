# Журнал изменений: registro-civil-bot

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/). Изменения монорепозитория в
целом — в корневом `CHANGELOG.md`; правил для AI-ассистентов — в `.ai/CHANGELOG.md`.

## [Не выпущено]

### Добавлено

- **Этап 1 «Бизнес-логика» (P1–P7).** Конфиг с жёстким минимумом интервала, секреты в
  keyring, домен и детектор, логи с вырезанием секретов, SQLite и миграция `0001`; вход через
  Playwright и `SessionManager`; `DomSource`, каркас `ApiSource`, разведка; цикл опроса с
  лестницей D4 и отступлением; Telegram, ntfy и CallMeBot с повторами до «Вижу»;
  Telegram-интерфейс для владельца; один экземпляр, защита от сна, очистка, отчёт, «сторож»;
  CLI `rcbot` (init, doctor, discover, login, check, run, simulate, purge, uninstall) и скрипты
  Планировщика. На живом сайте не проверялось (этап 3).

- Каталог бота в монорепозитории, ТЗ (`SPEC.md`, без номеров заявки владельца), план по фазам,
  инварианты, решения R1–R4.
- Зависимости из SPEC.md 2.1 в общем `uv.lock`: aiohttp 3.14, aiogram 3.31, aiogram-dialog 2.6,
  SQLAlchemy 2.1 + aiosqlite, Alembic, Pydantic 2.13, pydantic-settings, tzdata, sentry-sdk,
  Playwright 1.65, keyring 25.7; для тестов — aioresponses.
- Дымовой тест стека `tests/test_stack.py` (включая запуск Chromium без сети).
- Настройка `RCBOT_TRAMITE_ID` (номер заявки для текста уведомления) в `.env.example`.
- План работ по пяти этапам владельца (решение R5): бизнес-логика, тесты, подключение, аудит,
  работа.
