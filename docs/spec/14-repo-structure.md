# 14. Структура репозитория

```
meetup-rsvp-bot/
├── README.md                    # кратко: что это, как запустить
├── CLAUDE.md, AGENTS.md         # точки входа для AI-ассистентов (AGENTS.md генерируется)
├── dev.py                       # единая точка входа для рутинных задач
├── task-checklist.md            # чек-лист текущей/последней задачи
├── .ai/, .claude/, .codex/      # правила, агенты и навыки для ассистентов
├── docs/
│   ├── spec/                    # эта спецификация, разбитая на разделы
│   ├── ROADMAP.md               # вехи и статус
│   ├── open-questions.md        # решения владельца и открытые вопросы
│   └── architecture/            # overview.md (что построено), invariants.md
├── pyproject.toml, uv.lock
├── Dockerfile, compose.yaml, .dockerignore
├── .env.example
├── .pre-commit-config.yaml
├── alembic.ini
├── alembic/versions/
├── .github/workflows/
│   ├── ci.yml                   # PR: ruff, mypy, pytest
│   └── deploy.yml               # main: сборка образа → GHCR → деплой по SSH
├── src/meetup_bot/
│   ├── __main__.py              # точка входа: python -m meetup_bot
│   ├── config.py                # pydantic-settings (env)
│   ├── clock.py                 # источник времени (подменяется в тестах)
│   ├── db/
│   │   ├── base.py, session.py, types.py   # UtcDateTime TypeDecorator, PRAGMA
│   │   └── models.py
│   ├── repositories/            # events, registrations, users, bans, settings, audit
│   ├── services/
│   │   ├── registration.py      # записаться, отменить, очередь
│   │   ├── events.py            # создание, правка, статусы, расчёт времени
│   │   ├── attendance.py        # явка и правило бана
│   │   ├── bans.py
│   │   ├── admins.py
│   │   └── settings.py          # чтение и запись настроек с валидацией
│   ├── renderer/post.py         # текст поста + клавиатура + хеш, контроль длины
│   ├── telegram/
│   │   ├── post_updater.py      # debounce-пересборка (9.4)
│   │   ├── notifier.py          # личка, служебный чат, ретраи
│   │   └── callbacks.py         # CallbackData-фабрики
│   ├── handlers/
│   │   ├── signup.py            # кнопки под постом
│   │   ├── inline.py            # inline_query + chosen_inline_result
│   │   ├── start.py             # /start, /help, deep links
│   │   └── errors.py
│   ├── dialogs/                 # aiogram-dialog: events, bans, settings, admins
│   ├── middlewares/             # сессия БД, upsert пользователя, проверка админа
│   ├── scheduler/               # tick + задачи из раздела 11
│   └── texts/defaults.py        # тексты по умолчанию (раздел 6)
└── tests/
    ├── conftest.py              # in-memory SQLite, FakeClock
    ├── services/                # основная масса тестов
    └── renderer/
```
