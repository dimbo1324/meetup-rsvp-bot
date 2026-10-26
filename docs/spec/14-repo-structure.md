# 14. Структура репозитория

Состояние на 26 октября 2026 года. Реальная картина слоёв и модулей — `docs/architecture/overview.md`.

```
meetup-rsvp-bot/
├── README.md                    # что умеет бот, запуск, развёртывание, обновление, откат, бэкапы
├── CONTRIBUTING.md              # как работать с репозиторием
├── CHANGELOG.md                 # журнал изменений
├── CLAUDE.md, AGENTS.md         # точки входа для AI-ассистентов (AGENTS.md генерируется)
├── dev.py                       # единая точка входа для рутинных задач
├── task-checklist.md            # чек-лист текущей/последней задачи
├── .ai/, .claude/, .codex/      # правила, агенты и навыки для ассистентов
├── scripts/sync_agents.py       # генерация AGENTS.md из .ai/
├── docs/
│   ├── spec/                    # эта спецификация, разбитая на разделы
│   ├── ROADMAP.md               # вехи и статус
│   ├── open-questions.md        # решения владельца, открытые вопросы, технический долг
│   ├── admin-guide.md           # памятка ведущего
│   ├── telegram-test-checklist.md  # ручная проверка в Telegram
│   └── architecture/            # overview.md (что построено), invariants.md
├── pyproject.toml, uv.lock
├── Dockerfile, compose.yaml, .dockerignore
├── docker/entrypoint.sh         # проверка прав на data, umask, миграции, запуск
├── .env.example
├── .pre-commit-config.yaml
├── alembic.ini
├── alembic/versions/            # миграции 0001–0006
├── .github/workflows/
│   └── ci.yml                   # PR и main: dev.py quality-gate. deploy.yml (GHCR → SSH) ещё не создан
├── src/meetup_bot/
│   ├── __main__.py              # точка входа: python -m meetup_bot (коды выхода 0, 1, 2)
│   ├── app.py                   # сборка Bot/Dispatcher, запуск планировщика и long polling
│   ├── container.py             # корень сборки: связывает сервисы и задачи планировщика
│   ├── config.py                # pydantic-settings (env)
│   ├── clock.py                 # источник времени (SystemClock, FakeClock, ShiftableClock)
│   ├── user_info.py             # UserInfo — значение, общее для слоёв
│   ├── db/                      # base, session (PRAGMA, BEGIN IMMEDIATE), types (UtcDateTime), models
│   ├── repositories/            # admins, audit, bans, event_posts, events, events_admin,
│   │                            #   registrations, schedule, settings, users, users_known
│   ├── services/                # бизнес-логика (без Telegram):
│   │   ├── registration.py, waitlist.py   # запись, отмена, очередь
│   │   ├── events.py, event_results.py, event_validation.py, event_template.py, timing.py
│   │   ├── lifecycle.py                    # закрытие, завершение, автосоздание
│   │   ├── attendance.py, attendance_prompts.py, bans.py
│   │   ├── participants.py, admins.py, users.py, access.py, names.py, export.py
│   │   ├── reminders.py, post_reminders.py
│   │   ├── settings.py, settings_schema.py, defaults.py, rules.py
│   │   ├── posts.py                        # данные поста, предпросмотр, регистрация постов
│   │   └── cooldown.py, locks.py, safe.py, ports.py, provided.py, admin_support.py, errors.py
│   ├── renderer/post.py         # текст поста + клавиатура + хеш, контроль длины
│   ├── telegram/                # post_updater, notifier, posts, keyboards, callbacks, commands,
│   │                            #   attendance_ui, attendance_callbacks, service_chat, errors_text, common
│   ├── handlers/                # signup, inline, start, attendance, errors, fallback
│   ├── dialogs/                 # aiogram-dialog: main_menu, events, event_edit, event_wizard(+support),
│   │                            #   participants, bans, admins, settings(+view), picker, calendar_ru,
│   │                            #   states, _common, _buttons, _rights, intent_ids
│   ├── middlewares/             # user (данные пользователя), rate_limit (5 событий за 2 секунды)
│   ├── scheduler/               # tick, tasks (10 задач раздела 11), backup
│   └── texts/                   # defaults (text.*), render (подстановки), service (служебные сообщения)
└── tests/
    ├── conftest.py, helpers.py, tg_fake.py   # фикстуры, приложение на фейковых часах, фейковый Telegram
    ├── services/, scheduler/, telegram/, dialogs/, renderer/, texts/
    └── test_architecture.py, test_alembic.py, test_infra.py, test_config.py, test_db.py, ...
```
