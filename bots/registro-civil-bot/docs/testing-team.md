# Протокол команды тестов (этап 2) — ВРЕМЕННЫЙ документ

Удаляется тимлидом при интеграции. Общая картина тестов — `docs/testing.md`.

## Роли и зоны

Тимлид готовит фундамент, принимает работу и сливает ветки. Девять исполнителей (T1–T9) работают
**параллельно**, каждый в своём git worktree и своей ветке; зоны не пересекаются.

| № | Ветка | Зона тестов | Зона исправлений кода (`src/rcbot/…`) |
|---|---|---|---|
| T1 | `test/rcbot-t1-domain-config` | `tests/unit/domain/`, `tests/unit/config/`, `tests/unit/messaging/` | `domain/`, `config/`, `application/runtime.py`, `messages.py`, `status.py`, `ports.py`, `infrastructure/clock.py` |
| T2 | `test/rcbot-t2-poll-loop` | `tests/unit/loop/` | `application/poll_loop.py`, `session_manager.py`, `bus.py`, `supervisor.py`, `liveness.py`, `journal_feed.py` |
| T3 | `test/rcbot-t3-alerting` | `tests/unit/alerts/` | `application/alerting.py`, `incidents.py`, `control.py`, `housekeeping.py`, `health.py` |
| T4 | `test/rcbot-t4-gubuy` | `tests/unit/gubuy/`, `tests/integration/gubuy/` | `infrastructure/gubuy/`, `infrastructure/http/` |
| T5 | `test/rcbot-t5-channels-telegram` | `tests/unit/notify/`, `tests/unit/bot/` | `infrastructure/notify/`, `bot/` |
| T6 | `test/rcbot-t6-storage` | `tests/unit/storage/`, `tests/integration/storage/` | `infrastructure/db/`, `infrastructure/journal/`, `infrastructure/files.py`, `infrastructure/logging/setup.py` |
| T7 | `test/rcbot-t7-process-cli` | `tests/unit/cli/`, `tests/integration/process/` | `cli/`, `bootstrap.py`, `runner.py`, `infrastructure/win/`, `infrastructure/sentry.py`, `scripts/` бота |
| T8 | `test/rcbot-t8-privacy-arch` | `tests/privacy/`, `tests/architecture/` | `infrastructure/logging/redaction.py` |
| T9 | `test/rcbot-t9-e2e` | `tests/e2e/` | (кода не правит: находки — в отчёт и xfail) |

## Протокол

1. В своём worktree: `git switch -c <ветка из таблицы>`; затем из корня worktree
   `uv sync --all-packages --all-groups`. Браузер общий: перед тестами с Chromium задайте
   `PLAYWRIGHT_BROWSERS_PATH=C:\Users\dimbo1324\Desktop\from git\meetup-rsvp-bot\bots\registro-civil-bot\.browsers`.
2. **Коммитьте по шагам** (после каждого законченного куска: модуль, сценарий, исправление), не
   одним коммитом в конце. Сообщения — по-русски: `test(rcbot): что проверено` или
   `fix(rcbot): что исправлено`; в конце сообщения строка
   `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
3. Никогда: пушить, сливать, менять `main`, удалять ветки, трогать чужие зоны, `tests/fakes.py`,
   `tests/conftest.py`, `pyproject.toml`, `uv.lock`, `dev.py`, документы. Нужна правка общего —
   сделайте локальный помощник в своём каталоге тестов и напишите предложение в отчёте.
4. **Находка в коде своей зоны** — это цель работы. Сначала падающий регрессионный тест, затем
   минимальное исправление отдельным коммитом `fix(rcbot): …`. **Находка вне зоны** — не
   исправляйте: оформите тест с `@pytest.mark.xfail(strict=True, reason="BUG-T<№>: суть")` и
   опишите в отчёте (файл, строка, сценарий, ожидаемое поведение); тимлид исправит и снимет метку.
5. Запрещено ослаблять, отключать и удалять тесты или проверки, чтобы позеленить прогон, менять
   ожидание теста под сломанный код, ставить пустые `assert`, тестировать только «не упало».

## Качество — уровень Senior+

- Тест проверяет **поведение по требованиям** (SPEC 0.3, 2–3, 6–7; `docs/architecture/invariants.md`),
  а не пересказывает реализацию. Для каждого важного теста спросите: «упадёт ли он, если код
  сломать?». Проверяйте границы (10 с, 3 пустые проверки, 6 входов в час, 20 файлов, 10/5 ГБ),
  отрицательные пути, ошибки, отмену задач, повторный вызов, гонки, пустые и странные входы.
- Свойства (`hypothesis`) — там, где есть инвариант на всё множество входов (детектор, разбор,
  редактор секретов, расписание, квота).
- Время — только `FakeClock`/`ManualClock`; реальных пауз больше 0,05 с нет (исключение — тесты
  настоящих таймаутов с малыми значениями). Сеть — только `LocalTlsServer` или фейки. Живые
  адреса запрещены. Данные синтетические; настоящих номеров заявки, этапа, паролей и токенов нет.
- Детерминизм: тест не зависит от порядка, часового пояса машины, времени суток, скорости ПК;
  прогоните свою зону 3 раза подряд. Каждый тест быстрее 5 с (браузерные — быстрее 20 с).
- Комментарии, докстринги и имена тестов — по-русски (идентификаторы — по-английски); комментарий
  объясняет «почему», а не пересказывает код.

## Как проверять свою работу

```powershell
# из корня worktree
uv run python dev.py test --bot registro-civil-bot tests/unit/<ваш каталог>      # без покрытия
cd bots\registro-civil-bot
uv run --all-packages pytest tests/unit/<каталог> --cov=rcbot.<пакет> --cov-branch --cov-report=term-missing
cd ..\..
uv run ruff format bots/registro-civil-bot/tests/<ваши пути>
uv run ruff check bots/registro-civil-bot --output-format concise
uv run python dev.py typecheck --bot registro-civil-bot        # если правили src
```

`dev.py quality-gate` в worktree **не запускайте**: порог покрытия 85 % достижим только после
интеграции всех зон. Целевое покрытие вашей зоны — **≥ 90 %** (строки и ветки) для модулей
`domain` и `application`, для остальных — все осмысленные ветки; недостижимые строки назовите в
отчёте.

## Итоговый отчёт (не длиннее 400 слов)

Ветка и число коммитов; файлы и число тестов; покрытие по модулям зоны; найденные и исправленные
ошибки (с коммитами); найденные вне зоны (BUG-T<№>); предложения по общим фейкам/фикстурам;
что не удалось проверить и почему.
