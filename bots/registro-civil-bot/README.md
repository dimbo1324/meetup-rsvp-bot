# registro-civil-bot (rcbot)

Круглосуточно следит за календарём записи в Registro Civil (Уругвай, `bpmgob.mec.gub.uy`) и
мгновенно сообщает владельцу о свободных датах: в Telegram, push через ntfy и звонком через
CallMeBot. Записывается владелец сам — бот только читает календарь.

Полное ТЗ — [SPEC.md](SPEC.md). План и прогресс — [docs/ROADMAP.md](docs/ROADMAP.md).
Что нельзя ломать — [docs/architecture/invariants.md](docs/architecture/invariants.md).
Как устроен код — [docs/architecture/overview.md](docs/architecture/overview.md).

> **Состояние:** код бота написан (этап 1), на живом сайте и реальном Telegram ещё не
> запускался. Дальше: тесты на синтетических данных (этап 2), подключение (этап 3), аудит
> (этап 4), работа (этап 5) — [ROADMAP](docs/ROADMAP.md).

## Платформа

Один ноутбук с Windows 10/11, Python 3.14, `uv`. Бот — часть монорепозитория: всё, что к нему
относится, лежит в этом каталоге (`data/`, `logs/`, `.browsers/`, `.cache/` в git не попадают).
Вне каталога — только секреты в Windows Credential Manager и задание Планировщика.

## Подготовка окружения

Из **корня** монорепозитория:

```powershell
uv run python dev.py setup --bot registro-civil-bot   # зависимости, .env, Chromium в .browsers/
uv run --all-packages rcbot init                      # секреты, тема ntfy, ваш chat_id
uv run --all-packages rcbot doctor                    # проверка всего
```

`setup` создаёт `bots/registro-civil-bot/.env` из `.env.example`. В `.env` — только несекретные
настройки: номер этапа в `RCBOT_AGENDA_URL` и номер заявки в `RCBOT_TRAMITE_ID`. Секреты
(номер документа, пароль gub.uy, токен Telegram, тема ntfy, @username для звонков) в файлы не
пишутся: их спрашивает `rcbot init` и кладёт в Windows Credential Manager.

## Команды

Все — из корня репозитория через `uv run --all-packages rcbot <команда>`:

| Команда | Что делает |
|---|---|
| `init` | Мастер: секреты в Credential Manager, тема ntfy, определение вашего chat_id |
| `doctor [--offline] [--full]` | Версии, Chromium, секреты, настройки, OneDrive, доступ к хостам, часы; `--full` — ещё `pip-audit` |
| `discover` | Разведка: окно браузера, вы входите сами, бот записывает очищенные запросы календаря |
| `login [--check]` | Вход на сайт; `--check` — пускает ли сайт сразу на шаг «Agenda» |
| `check --once` | Одна проверка календаря |
| `run` | Постоянная работа (для отладки в консоли; в обычной жизни — через Планировщик) |
| `simulate [--wait 600]` | Тестовая тревога во все каналы с повторами до «Вижу» |
| `purge` | Удалить логи, временные файлы, карантин, снимки, дампы, кеш |
| `uninstall` | Удалить секреты из Credential Manager и задание Планировщика |

Автозапуск: `powershell -ExecutionPolicy Bypass -File bots\registro-civil-bot\scripts\register_task.ps1`
(при входе в Windows, без окна, перезапуск при сбое). В настройках питания от сети: «Переводить
в спящий режим» — «Никогда», закрытие крышки — «Ничего не делать».

## Управление из Telegram

`/status`, `/pause [минуты]`, `/resume`, `/interval <сек>` (не меньше 10), `/test`, `/last`,
`/relogin`, `/cleanup`, `/menu`. Тревога подтверждается кнопкой «✅ Вижу» или словом «ок».
Если сайт потребует шаг 1 с контрольным вопросом, бот попросит пройти его в вашем браузере и
нажать «Готово» — сам он на вопрос не отвечает.

## Что владелец делает сам (SPEC.md, 9.1)

1. Создать бота у @BotFather и написать ему `/start`.
2. Поставить на телефон приложение ntfy.
3. Написать `/start` боту @CallMeBot_txtbot и разрешить звонки.
4. Когда попросит ассистент: ввести секреты в `rcbot init`, один раз войти в gub.uy в окне
   `rcbot discover`, подписаться на тему ntfy, ответить на тестовый звонок.
