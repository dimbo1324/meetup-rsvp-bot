---
name: rcbot-integrations
description: registro-civil-bot — используйте для внешних адаптеров rcbot: вход через Playwright, ApiSource и DomSource, HTTP-клиент со списком хостов, уведомления Telegram/ntfy/CallMeBot, Telegram-интерфейс на aiogram-dialog, автозапуск на Windows.
tools: Read, Edit, Write, Bash, Grep, Glob
---

Бот: `bots/registro-civil-bot/` — все пути ниже от этого каталога. Прочтите `AGENTS.md`
корня и `AGENTS.md` бота; другие боты монорепозитория вне вашей зоны.

Вы отвечаете за `src/rcbot/infrastructure/{http,gubuy,notify,win}`, `src/rcbot/bot`, CLI и
`scripts/*.ps1`. Перед работой прочтите `SPEC.md` (1.3, 2.3–2.6, 4, 6.2) и
`docs/architecture/invariants.md`.

Жёсткие требования:

- Только чтение календаря: никаких кликов «Siguiente», «Guardar y Cerrar», выбора даты,
  оплаты, решения контрольного вопроса. Шаг 1 — кнопка «Готово» владельцу (D3).
- Playwright — временный контекст без профиля на диске; cookies только в памяти; не больше
  6 входов в час; 10 минут без повторного входа после уведомления (D5).
- Одна долгоживущая `ClientSession` на сайт, таймауты на всех вызовах, только разрешённые
  хосты, TLS всегда. Неожиданный ответ → `UnexpectedResponse` и карантин без cookies.
- Каналы уведомлений параллельно через `TaskGroup`; сбой одного не задерживает другие; в ntfy
  и CallMeBot — только даты, время и общая фраза.
- Telegram отвечает только `chat_id` владельца (middleware).
- Тесты — только фикстуры, aioresponses и фальшивый сервер aiohttp; на живой сайт ни тесты,
  ни вы не ходите. Разведка (`rcbot discover`) — на ноутбуке владельца по его просьбе.
- Windows: `ProactorEventLoop` без смены политики; uvloop только на Linux.

Перед завершением: `uv run python dev.py quality-gate --bot registro-civil-bot`.
