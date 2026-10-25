"""Scheduler: тик, изоляция ошибок задач, таймаут, пульс, run_forever; сквозные кейсы 15 и 16."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from meetup_bot.clock import FakeClock
from meetup_bot.db.models import Event, EventStatus
from meetup_bot.repositories import users
from meetup_bot.scheduler.tick import DEFAULT_TASK_TIMEOUT, Scheduler
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

STARTS_AT = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
REMINDER_AT = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 10, 24, 17, 0, tzinfo=UTC)
PROMPT_AT = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


class FakeTask:
    def __init__(self, name: str, count: int = 1, *, error: Exception | None = None) -> None:
        self.name = name
        self.count = count
        self.error = error
        self.seen: list[datetime] = []

    async def run(self, now: datetime) -> int:
        self.seen.append(now)
        if self.error is not None:
            raise self.error
        return self.count


class HangingTask:
    name = "hanging"

    async def run(self, now: datetime) -> int:
        await asyncio.sleep(3600)
        return 1


# ---- Scheduler.tick -------------------------------------------------------------------


async def test_тик_возвращает_счётчики_по_именам_задач(clock: FakeClock, tmp_path: Path) -> None:
    scheduler = Scheduler(
        clock, [FakeTask("a", 2), FakeTask("b", 0)], heartbeat_path=tmp_path / "hb"
    )
    assert await scheduler.tick() == {"a": 2, "b": 0}


async def test_тик_передаёт_задачам_время_часов(clock: FakeClock, tmp_path: Path) -> None:
    task = FakeTask("a")
    scheduler = Scheduler(clock, [task], heartbeat_path=tmp_path / "hb")
    await scheduler.tick()
    clock.advance(timedelta(minutes=1))
    await scheduler.tick()
    assert task.seen == [
        datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 23, 12, 1, tzinfo=UTC),
    ]


async def test_тик_без_задач_возвращает_пустой_результат(clock: FakeClock, tmp_path: Path) -> None:
    assert await Scheduler(clock, [], heartbeat_path=tmp_path / "hb").tick() == {}


async def test_ошибка_одной_задачи_не_мешает_остальным(
    clock: FakeClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    first = FakeTask("first")
    broken = FakeTask("broken", error=RuntimeError("сломана"))
    last = FakeTask("last", 3)
    scheduler = Scheduler(clock, [first, broken, last], heartbeat_path=tmp_path / "hb")
    with caplog.at_level(logging.ERROR):
        result = await scheduler.tick()
    assert result == {"first": 1, "last": 3}
    assert "broken" in caplog.text
    assert last.seen


async def test_после_ошибки_задача_запускается_на_следующем_тике(
    clock: FakeClock, tmp_path: Path
) -> None:
    broken = FakeTask("broken", error=RuntimeError("сломана"))
    scheduler = Scheduler(clock, [broken], heartbeat_path=tmp_path / "hb")
    await scheduler.tick()
    await scheduler.tick()
    assert len(broken.seen) == 2


async def test_зависшая_задача_прерывается_по_таймауту_и_не_мешает_остальным(
    clock: FakeClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    after = FakeTask("after", 4)
    scheduler = Scheduler(
        clock, [HangingTask(), after], heartbeat_path=tmp_path / "hb", task_timeout=0.05
    )
    with caplog.at_level(logging.ERROR):
        result = await scheduler.tick()
    assert result == {"after": 4}
    assert "по таймауту" in caplog.text


def test_таймаут_задачи_по_умолчанию_разумный() -> None:
    assert 0 < DEFAULT_TASK_TIMEOUT <= 600


async def test_тик_пишет_пульс_со_временем_часов(clock: FakeClock, tmp_path: Path) -> None:
    heartbeat = tmp_path / "nested" / "heartbeat"
    await Scheduler(clock, [], heartbeat_path=heartbeat).tick()
    assert heartbeat.read_text(encoding="utf-8") == clock.now().isoformat()


async def test_пульс_пишется_даже_если_все_задачи_упали(clock: FakeClock, tmp_path: Path) -> None:
    heartbeat = tmp_path / "hb"
    scheduler = Scheduler(clock, [FakeTask("a", error=RuntimeError("x"))], heartbeat_path=heartbeat)
    await scheduler.tick()
    assert heartbeat.exists()


async def test_пульс_обновляется_на_каждом_тике(clock: FakeClock, tmp_path: Path) -> None:
    heartbeat = tmp_path / "hb"
    scheduler = Scheduler(clock, [], heartbeat_path=heartbeat)
    await scheduler.tick()
    clock.advance(timedelta(minutes=1))
    await scheduler.tick()
    assert heartbeat.read_text(encoding="utf-8") == "2026-10-23T12:01:00+00:00"


async def test_сбой_записи_пульса_не_роняет_тик(
    clock: FakeClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    scheduler = Scheduler(clock, [FakeTask("a")], heartbeat_path=blocker / "hb")
    with caplog.at_level(logging.ERROR):
        assert await scheduler.tick() == {"a": 1}
    assert "пульс" in caplog.text


def test_до_следующей_границы_минуты(clock: FakeClock, tmp_path: Path) -> None:
    scheduler = Scheduler(clock, [], heartbeat_path=tmp_path / "hb")
    assert scheduler._seconds_to_next_boundary() == 60.0
    clock.advance(timedelta(seconds=20))
    assert scheduler._seconds_to_next_boundary() == 40.0
    clock.advance(timedelta(seconds=39))
    assert scheduler._seconds_to_next_boundary() == 1.0


# ---- run_forever ----------------------------------------------------------------------


class CountingTask:
    name = "counting"

    def __init__(self, stop_after: int) -> None:
        self.runs = 0
        self.done = asyncio.Event()
        self._stop_after = stop_after

    async def run(self, now: datetime) -> int:
        self.runs += 1
        if self.runs >= self._stop_after:
            self.done.set()
        return 0


async def test_run_forever_делает_несколько_тиков_и_останавливается_отменой(
    clock: FakeClock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    task = CountingTask(stop_after=3)
    scheduler = Scheduler(clock, [task], interval=0.01, heartbeat_path=tmp_path / "hb")
    runner = asyncio.create_task(scheduler.run_forever())
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(task.done.wait(), timeout=10)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
    assert task.runs >= 3
    assert "планировщик остановлен" in caplog.text


async def test_run_forever_переживает_сбой_самого_тика(
    clock: FakeClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Scheduler(clock, [], interval=0.01, heartbeat_path=tmp_path / "hb")
    calls = 0
    done = asyncio.Event()

    async def flaky_tick() -> dict[str, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("сбой цикла")
        if calls >= 3:
            done.set()
        return {}

    monkeypatch.setattr(scheduler, "tick", flaky_tick)
    runner = asyncio.create_task(scheduler.run_forever())
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(done.wait(), timeout=10)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
    assert calls >= 3
    assert "сбой тика планировщика" in caplog.text


# ---- сквозные сценарии на настоящем контейнере ----------------------------------------


def go_to(app: App, moment: datetime) -> None:
    app.clock.advance(moment - app.clock.now())


async def count_events(app: App) -> int:
    async with app.sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(Event)) or 0)


async def only_event(app: App) -> Event:
    async with app.sessions() as session:
        event = await session.scalar(select(Event))
    assert event is not None
    return event


async def join(app: App, event_id: int, n: int) -> None:
    await app.services.registration.register(event_id, person(n))
    async with app.sessions() as session, session.begin():
        await users.set_dm_available(session, person(n).id, True, app.clock.now())


async def test_двойной_тик_не_создаёт_встречу_дважды(app: App) -> None:  # кейс 15
    first = await app.services.scheduler.tick()
    second = await app.services.scheduler.tick()
    assert first["auto_create"] == 1
    assert second["auto_create"] == 0
    assert await count_events(app) == 1
    assert (await only_event(app)).recurrence_key == "weekly:2026-10-24"
    assert app.notifier.service.count("Создана встреча на суббота, 24 октября, 17:00.") == 1


async def test_тики_каждую_минуту_не_плодят_встречи(app: App) -> None:  # кейс 15
    for _ in range(30):
        await app.services.scheduler.tick()
        app.clock.advance(timedelta(minutes=1))
    assert await count_events(app) == 1


async def test_двойной_тик_не_шлёт_напоминание_дважды(app: App) -> None:  # кейс 15
    await app.services.scheduler.tick()
    event = await only_event(app)
    await join(app, event.id, 1)
    go_to(app, REMINDER_AT)
    first = await app.services.scheduler.tick()
    second = await app.services.scheduler.tick()
    assert (first["reminder"], second["reminder"]) == (1, 0)
    assert len(app.notifier.dms_to(person(1).id)) == 1


async def test_двойной_тик_закрывает_запись_один_раз(app: App) -> None:  # кейс 15
    await app.services.settings.set("registration_close_hours_before", 3, ADMIN_ID)
    await app.services.scheduler.tick()
    event = await only_event(app)
    await join(app, event.id, 1)
    go_to(app, CLOSES_AT)
    first = await app.services.scheduler.tick()
    second = await app.services.scheduler.tick()
    assert (first["close_registration"], second["close_registration"]) == (1, 0)
    summaries = [t for t in app.notifier.service if t.startswith("Запись на встречу")]
    assert len(summaries) == 1


async def test_двойной_тик_завершает_встречу_один_раз(app: App) -> None:  # кейс 15
    await app.services.scheduler.tick()
    go_to(app, STARTS_AT)
    first = await app.services.scheduler.tick()
    second = await app.services.scheduler.tick()
    assert (first["finish_events"], second["finish_events"]) == (1, 0)
    assert (await only_event(app)).status == EventStatus.FINISHED.value


async def test_двойной_тик_шлёт_запрос_явки_один_раз(app: App) -> None:  # кейс 15
    await app.services.scheduler.tick()
    event = await only_event(app)
    go_to(app, PROMPT_AT)
    first = await app.services.scheduler.tick()
    second = await app.services.scheduler.tick()
    assert (first["attendance_prompt"], second["attendance_prompt"]) == (1, 0)
    assert sorted(app.notifier.prompts) == [(OWNER_ID, event.id), (ADMIN_ID, event.id)]


async def test_двойной_тик_закрывает_окно_явки_один_раз(app: App) -> None:  # кейс 15
    await app.services.scheduler.tick()
    go_to(app, STARTS_AT + timedelta(days=7, hours=1))
    first = await app.services.scheduler.tick()
    second = await app.services.scheduler.tick()
    assert (first["attendance_expire"], second["attendance_expire"]) == (1, 0)


async def test_без_автозакрытия_один_тик_в_момент_начала_закрывает_и_завершает(
    app: App,
) -> None:
    await app.services.scheduler.tick()
    event = await only_event(app)
    await join(app, event.id, 1)
    go_to(app, event.starts_at)
    first = await app.services.scheduler.tick()
    second = await app.services.scheduler.tick()
    assert (first["close_registration"], first["finish_events"]) == (1, 1)
    assert (second["close_registration"], second["finish_events"]) == (0, 0)
    assert (await only_event(app)).status == EventStatus.FINISHED.value


async def test_тик_после_простоя_в_2_суток_догоняет_закрытие_и_завершение(
    app: App,
) -> None:  # кейс 16
    await app.services.scheduler.tick()
    event = await only_event(app)
    await join(app, event.id, 1)
    app.notifier.service.clear()
    app.clock.advance(timedelta(days=2))  # воскресенье 09:00 по Монтевидео

    done = await app.services.scheduler.tick()

    after = await only_event(app)
    assert after.status == EventStatus.FINISHED.value
    assert after.closed_at == app.clock.now()
    assert (done["close_registration"], done["finish_events"]) == (1, 1)
    assert await count_events(app) == 1


async def test_тик_после_простоя_пропускает_протухшее_напоминание_с_отметкой(
    app: App,
) -> None:  # кейс 16
    await app.services.scheduler.tick()
    event = await only_event(app)
    await join(app, event.id, 1)
    app.clock.advance(timedelta(days=2))

    done = await app.services.scheduler.tick()

    assert done["reminder"] == 0
    assert app.notifier.dms == []
    assert (await only_event(app)).reminder_sent_at == app.clock.now()


async def test_тик_после_простоя_не_шлёт_итог_закрытия_уже_прошедшей_встречи(
    app: App,
) -> None:  # кейс 16
    await app.services.scheduler.tick()
    app.notifier.service.clear()
    app.clock.advance(timedelta(days=2))
    await app.services.scheduler.tick()
    assert [t for t in app.notifier.service if t.startswith("Запись на встречу")] == []


async def test_тик_после_простоя_шлёт_запрос_явки_если_окно_открыто(app: App) -> None:  # кейс 16
    await app.services.scheduler.tick()
    event = await only_event(app)
    app.clock.advance(timedelta(days=2))
    done = await app.services.scheduler.tick()
    assert done["attendance_prompt"] == 1
    assert sorted(app.notifier.prompts) == [(OWNER_ID, event.id), (ADMIN_ID, event.id)]


async def test_тик_после_простоя_в_9_суток_закрывает_окно_явки_без_запроса(
    app: App,
) -> None:  # кейс 16
    await app.services.scheduler.tick()
    event = await only_event(app)
    await join(app, event.id, 1)
    app.clock.advance(timedelta(days=9))

    done = await app.services.scheduler.tick()

    after = await only_event(app)
    assert after.status == EventStatus.FINISHED.value
    assert after.attendance_prompt_sent_at == app.clock.now()
    assert app.notifier.prompts == []
    assert done["attendance_expire"] == 1
    assert after.attendance_marked_at == app.clock.now()
    assert app.notifier.dms == []


async def test_тик_после_простоя_не_создаёт_встречу_задним_числом(app: App) -> None:  # кейс 16
    app.clock.advance(timedelta(days=9))  # воскресенье 1 ноября 09:00 по Монтевидео
    done = await app.services.scheduler.tick()
    assert done["auto_create"] == 0
    assert await count_events(app) == 0


async def test_контейнер_собирает_задачи_в_нужном_порядке(app: App) -> None:
    done = await app.services.scheduler.tick()
    assert list(done) == [
        "auto_create", "reminder", "close_registration", "finish_events",
        "attendance_prompt", "attendance_reminder", "attendance_expire", "ban_recheck",
        "backup",
    ]  # fmt: skip


async def test_тик_контейнера_пишет_пульс(app: App) -> None:
    await app.services.scheduler.tick()
    assert (app.tmp_path / "heartbeat").read_text(encoding="utf-8") == app.clock.now().isoformat()
