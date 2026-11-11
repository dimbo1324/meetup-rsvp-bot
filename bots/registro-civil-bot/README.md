# registro-civil-bot (rcbot)

Круглосуточно следит за календарём записи в Registro Civil (Уругвай, `bpmgob.mec.gub.uy`) и
мгновенно сообщает владельцу о свободных датах: в Telegram, push через ntfy и звонком через
CallMeBot. Записывается владелец сам — бот только читает календарь.

Полное ТЗ — [SPEC.md](SPEC.md). План и прогресс — [docs/ROADMAP.md](docs/ROADMAP.md).
Что нельзя ломать — [docs/architecture/invariants.md](docs/architecture/invariants.md).

> **Состояние:** репозиторий подготовлен, стек установлен и проверен; код ещё не написан.
> Работа идёт пятью этапами: бизнес-логика → тесты → подключение → аудит → работа
> ([ROADMAP](docs/ROADMAP.md)). Команды `rcbot …` из SPEC.md появятся на первом этапе.

## Платформа

Один ноутбук с Windows 10/11, Python 3.14, `uv`. Бот — часть монорепозитория: всё, что к нему
относится, лежит в этом каталоге (`data/`, `logs/`, `.browsers/`, `.cache/` в git не попадают).

## Подготовка окружения

Из **корня** монорепозитория:

```powershell
uv run python dev.py setup --bot registro-civil-bot   # зависимости, .env, Chromium в .browsers/
uv run python dev.py doctor
uv run python dev.py quality-gate --bot registro-civil-bot
```

`setup` создаёт `bots/registro-civil-bot/.env` из `.env.example`. В `.env` — только несекретные
настройки; подставьте номер этапа своей заявки в `RCBOT_AGENDA_URL`. Секреты (пароль gub.uy,
номер документа, токены) в файлы не пишутся: их спросит мастер `rcbot init` и положит в Windows
Credential Manager.

## Что владелец делает сам (SPEC.md, 9.1)

1. Создать бота у @BotFather и написать ему `/start`.
2. Поставить на телефон приложение ntfy.
3. Написать `/start` боту @CallMeBot_txtbot и разрешить звонки.
4. Когда попросит ассистент: ввести секреты в `rcbot init`, один раз войти в gub.uy в окне
   `rcbot discover`, подписаться на тему ntfy, ответить на тестовый звонок.
