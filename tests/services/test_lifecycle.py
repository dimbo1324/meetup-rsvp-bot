"""LifecycleService: закрытие записи, завершение, автосоздание еженедельной встречи (5.1, 5.9)."""

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from meetup_bot.clock import FakeClock
from meetup_bot.db.models import AuditLog, Event, EventStatus
from meetup_bot.repositories.users import UserInfo
from meetup_bot.services.lifecycle import (
    AutoCreateStatus,
    EventDefaults,
    LifecycleService,
    last_auto_create_moment,
)
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.registration import RegisterStatus
from meetup_bot.services.rules import RulesSettings
from tests.helpers import ADMIN_ID, OWNER_ID, App, RecordingNotifier, RecordingRefresher, person

# Пятница 2026-10-23 12:00 UTC; встреча по умолчанию — суббота 24.10 17:00 (20:00 UTC)
STARTS_AT = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)
CLOSES_AT = datetime(2026, 10, 24, 17, 0, tzinfo=UTC)


async def create_event(app: App, capacity: int = 10) -> int:
    return (await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)).event_id


async def join(app: App, event_id: int, user: UserInfo) -> None:
    result = await app.services.registration.register(event_id, user)
    assert result.status is RegisterStatus.OK


async def get_event(app: App, event_id: int) -> Event:
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        return event


async def audit_actions(app: App, event_id: int | None = None) -> list[str]:
    async with app.sessions() as session:
        query = select(AuditLog.action).order_by(AuditLog.id)
        if event_id is not None:
            query = query.where(AuditLog.event_id == event_id)
        return list((await session.scalars(query)).all())


async def count_events(app: App) -> int:
    async with app.sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(Event)) or 0)


def to_closing(app: App) -> None:
    """Часы на момент закрытия записи встречи по умолчанию."""
    app.clock.advance(CLOSES_AT - app.clock.now())


def to_start(app: App) -> None:
    app.clock.advance(STARTS_AT - app.clock.now())


class FailingNotifier(RecordingNotifier):
    async def send_service(self, text: str) -> None:
        raise RuntimeError("служебный чат недоступен")


class FailingRefresher(RecordingRefresher):
    def schedule(self, event_id: int) -> None:
        raise RuntimeError("очередь недоступна")


# ---- close_due ------------------------------------------------------------------------


async def test_close_due_до_срока_ничего_не_делает(app: App) -> None:
    event_id = await create_event(app)
    to_closing(app)
    app.clock.advance(timedelta(seconds=-1))
    assert await app.services.lifecycle.close_due(app.clock.now()) == []
    assert (await get_event(app, event_id)).status == EventStatus.OPEN.value


async def test_close_due_в_момент_срока_закрывает_запись(app: App) -> None:
    event_id = await create_event(app)
    to_closing(app)
    assert await app.services.lifecycle.close_due(app.clock.now()) == [event_id]
    event = await get_event(app, event_id)
    assert event.status == EventStatus.CLOSED.value
    assert event.closed_at == CLOSES_AT
    assert event.updated_at == CLOSES_AT


async def test_close_due_пишет_аудит_от_планировщика(app: App) -> None:
    event_id = await create_event(app)
    to_closing(app)
    await app.services.lifecycle.close_due(app.clock.now())
    async with app.sessions() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "event.close"))
    assert row is not None
    assert (row.event_id, row.actor_id, row.payload) == (event_id, None, {"by": "scheduler"})


async def test_close_due_просит_пересобрать_посты(app: App) -> None:
    event_id = await create_event(app)
    app.refresher.scheduled.clear()
    to_closing(app)
    await app.services.lifecycle.close_due(app.clock.now())
    assert app.refresher.scheduled == [event_id]


async def test_close_due_идемпотентен(app: App) -> None:  # кейс 15
    event_id = await create_event(app)
    await join(app, event_id, person(1))
    to_closing(app)
    now = app.clock.now()
    assert await app.services.lifecycle.close_due(now) == [event_id]
    assert await app.services.lifecycle.close_due(now) == []
    assert (await audit_actions(app, event_id)).count("event.close") == 1
    assert len(app.notifier.service) == 1


async def test_close_due_итог_в_служебный_чат_содержит_список(app: App) -> None:
    event_id = await create_event(app, capacity=10)
    await join(app, event_id, UserInfo(1001, "Света"))
    await join(app, event_id, UserInfo(1002, "Иван", "Петров"))
    to_closing(app)
    await app.services.lifecycle.close_due(app.clock.now())
    assert app.notifier.service == [
        "Запись на встречу (суббота, 24 октября, 17:00) закрыта.\n"
        "Заполнено 2 из 10.\n"
        "1. Света\n"
        "2. Иван Петров"
    ]


async def test_close_due_пустая_встреча_тоже_шлёт_итог(app: App) -> None:
    await create_event(app, capacity=7)
    to_closing(app)
    await app.services.lifecycle.close_due(app.clock.now())
    assert len(app.notifier.service) == 1
    assert "Заполнено 0 из 7." in app.notifier.service[0]


async def test_close_due_итог_помечает_гостя(app: App) -> None:
    await app.services.settings.set("plus_one_enabled", True, ADMIN_ID)
    event_id = await create_event(app)
    result = await app.services.registration.register(event_id, person(1), guests=1)
    assert result.status is RegisterStatus.OK
    to_closing(app)
    await app.services.lifecycle.close_due(app.clock.now())
    assert "1. Участник1 (+1)" in app.notifier.service[0]
    assert "Заполнено 2 из 10." in app.notifier.service[0]


async def test_close_due_итог_экранирует_html_в_именах(app: App) -> None:
    event_id = await create_event(app)
    await join(app, event_id, UserInfo(1001, "<b>Хакер</b> & Ко"))
    to_closing(app)
    await app.services.lifecycle.close_due(app.clock.now())
    text = app.notifier.service[0]
    assert "1. &lt;b&gt;Хакер&lt;/b&gt; &amp; Ко" in text
    assert "<b>" not in text


async def test_close_due_длинный_итог_режется_на_сообщения(app: App) -> None:
    event_id = await create_event(app, capacity=200)
    names = [f"Участник номер {n:03d} " + "я" * 30 for n in range(150)]
    for name in names:
        await app.services.participants.admin_add(OWNER_ID, event_id, manual_name=name)
    to_closing(app)
    await app.services.lifecycle.close_due(app.clock.now())
    chunks = app.notifier.service
    assert len(chunks) > 1
    assert all(len(chunk) <= 3500 for chunk in chunks)
    joined = "\n".join(chunks)
    assert all(name in joined for name in names)
    assert joined.index("номер 000") < joined.index("номер 149")


async def test_close_due_не_трогает_отменённую_встречу(app: App) -> None:
    event_id = await create_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id, "дождь")
    to_closing(app)
    assert await app.services.lifecycle.close_due(app.clock.now()) == []
    assert (await get_event(app, event_id)).status == EventStatus.CANCELLED.value


async def test_close_due_не_закрывает_уже_закрытую_админом(app: App) -> None:
    event_id = await create_event(app)
    await app.services.events.set_registration_open(OWNER_ID, event_id, False)
    to_closing(app)
    assert await app.services.lifecycle.close_due(app.clock.now()) == []
    assert (await audit_actions(app, event_id)).count("event.close") == 1


async def test_close_due_закрывает_несколько_встреч_по_порядку(app: App) -> None:
    first = await create_event(app)
    second = (
        await app.services.events.create_event_as_admin(
            OWNER_ID, starts_at=STARTS_AT + timedelta(days=7)
        )
    ).event_id
    app.clock.advance(timedelta(days=9))
    assert await app.services.lifecycle.close_due(app.clock.now()) == [first, second]


async def test_close_due_после_простоя_закрывает_но_итог_не_шлёт(app: App) -> None:  # кейс 16
    event_id = await create_event(app)
    await join(app, event_id, person(1))
    app.clock.advance(timedelta(days=2))
    assert await app.services.lifecycle.close_due(app.clock.now()) == [event_id]
    assert (await get_event(app, event_id)).status == EventStatus.CLOSED.value
    assert app.notifier.service == []


async def test_close_due_сбой_служебного_чата_не_откатывает_статус(
    app: App, clock: FakeClock
) -> None:
    event_id = await create_event(app)
    service = LifecycleService(
        app.sessions,
        clock,
        EventLocks(),
        FailingNotifier(),
        app.refresher,
        schedule=app.services.settings.schedule_now,
    )
    to_closing(app)
    assert await service.close_due(clock.now()) == [event_id]
    assert (await get_event(app, event_id)).status == EventStatus.CLOSED.value


async def test_close_due_сбой_очереди_постов_не_откатывает_статус(
    app: App, clock: FakeClock
) -> None:
    event_id = await create_event(app)
    service = LifecycleService(
        app.sessions,
        clock,
        EventLocks(),
        app.notifier,
        FailingRefresher(),
        schedule=app.services.settings.schedule_now,
    )
    to_closing(app)
    assert await service.close_due(clock.now()) == [event_id]
    assert (await get_event(app, event_id)).status == EventStatus.CLOSED.value


async def test_close_due_пропускает_встречу_закрытую_между_выборкой_и_блокировкой(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await create_event(app)
    await app.services.events.cancel_event(OWNER_ID, event_id)
    to_closing(app)

    async def stale_ids(*_args: Any) -> list[int]:
        return [event_id]

    monkeypatch.setattr(app.services.lifecycle, "_due_ids", stale_ids)
    assert await app.services.lifecycle.close_due(app.clock.now()) == []
    assert (await get_event(app, event_id)).status == EventStatus.CANCELLED.value


async def test_close_due_пропускает_встречу_которую_перенесли_на_позже(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await create_event(app)

    async def stale_ids(*_args: Any) -> list[int]:
        return [event_id]

    monkeypatch.setattr(app.services.lifecycle, "_due_ids", stale_ids)
    # Момент закрытия ещё не наступил, хотя выборка вернула встречу
    assert await app.services.lifecycle.close_due(app.clock.now()) == []
    assert (await get_event(app, event_id)).status == EventStatus.OPEN.value


async def test_close_due_исчезнувшая_встреча_пропускается(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stale_ids(*_args: Any) -> list[int]:
        return [99999]

    monkeypatch.setattr(app.services.lifecycle, "_due_ids", stale_ids)
    assert await app.services.lifecycle.close_due(app.clock.now()) == []


# ---- finish_due -----------------------------------------------------------------------


async def test_finish_due_до_начала_ничего_не_делает(app: App) -> None:
    await create_event(app)
    app.clock.advance(STARTS_AT - app.clock.now() - timedelta(seconds=1))
    assert await app.services.lifecycle.finish_due(app.clock.now()) == []


async def test_finish_due_в_момент_начала_завершает_открытую(app: App) -> None:
    event_id = await create_event(app)
    to_start(app)
    assert await app.services.lifecycle.finish_due(app.clock.now()) == [event_id]
    event = await get_event(app, event_id)
    assert event.status == EventStatus.FINISHED.value
    assert event.updated_at == STARTS_AT


async def test_finish_due_завершает_закрытую(app: App) -> None:
    event_id = await create_event(app)
    to_closing(app)
    await app.services.lifecycle.close_due(app.clock.now())
    to_start(app)
    assert await app.services.lifecycle.finish_due(app.clock.now()) == [event_id]
    assert (await get_event(app, event_id)).status == EventStatus.FINISHED.value


async def test_finish_due_пишет_аудит_и_просит_пересобрать_пост(app: App) -> None:
    event_id = await create_event(app)
    app.refresher.scheduled.clear()
    to_start(app)
    await app.services.lifecycle.finish_due(app.clock.now())
    async with app.sessions() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "event.finish"))
    assert row is not None
    assert (row.event_id, row.actor_id, row.payload) == (event_id, None, {"by": "scheduler"})
    assert app.refresher.scheduled == [event_id]


async def test_finish_due_идемпотентен(app: App) -> None:  # кейс 15
    event_id = await create_event(app)
    to_start(app)
    now = app.clock.now()
    assert await app.services.lifecycle.finish_due(now) == [event_id]
    assert await app.services.lifecycle.finish_due(now) == []
    assert (await audit_actions(app, event_id)).count("event.finish") == 1


async def test_finish_due_не_трогает_отменённую_и_завершённую(app: App) -> None:
    cancelled = await create_event(app)
    await app.services.events.cancel_event(OWNER_ID, cancelled)
    app.clock.advance(timedelta(days=3))
    assert await app.services.lifecycle.finish_due(app.clock.now()) == []
    assert (await get_event(app, cancelled)).status == EventStatus.CANCELLED.value


async def test_finish_due_пропускает_устаревшую_выборку(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = await create_event(app)

    async def stale_ids(*_args: Any) -> list[int]:
        return [event_id, 99999]

    monkeypatch.setattr(app.services.lifecycle, "_due_ids", stale_ids)
    # Встреча ещё не началась: решающая проверка под блокировкой её не пропускает
    assert await app.services.lifecycle.finish_due(app.clock.now()) == []
    assert (await get_event(app, event_id)).status == EventStatus.OPEN.value


# ---- last_auto_create_moment ----------------------------------------------------------


def test_последний_момент_автосоздания_в_пятницу_это_прошлое_воскресенье() -> None:
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    moment = last_auto_create_moment(now, RulesSettings(), "America/Montevideo")
    assert moment.astimezone(UTC) == datetime(2026, 10, 18, 13, 0, tzinfo=UTC)


def test_последний_момент_ровно_в_момент_автосоздания_это_он_сам() -> None:
    now = datetime(2026, 10, 25, 13, 0, tzinfo=UTC)  # воскресенье 10:00 по Монтевидео
    moment = last_auto_create_moment(now, RulesSettings(), "America/Montevideo")
    assert moment.astimezone(UTC) == now


def test_последний_момент_за_минуту_до_это_прошлая_неделя() -> None:
    now = datetime(2026, 10, 25, 12, 59, tzinfo=UTC)
    moment = last_auto_create_moment(now, RulesSettings(), "America/Montevideo")
    assert moment.astimezone(UTC) == datetime(2026, 10, 18, 13, 0, tzinfo=UTC)


def test_последний_момент_учитывает_настроенные_день_и_время() -> None:
    rules = RulesSettings(auto_create_weekday=0, auto_create_time=time(8, 30))
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)  # пятница
    moment = last_auto_create_moment(now, rules, "UTC")
    assert moment == datetime(2026, 10, 19, 8, 30, tzinfo=UTC)


# ---- create_weekly_event --------------------------------------------------------------


async def test_автосоздание_создаёт_встречу_с_ключом_по_дате(app: App) -> None:
    result = await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert result.status is AutoCreateStatus.CREATED
    assert result.recurrence_key == "weekly:2026-10-24"
    assert result.event_id is not None
    event = await get_event(app, result.event_id)
    assert event.starts_at == STARTS_AT
    assert event.recurrence_key == "weekly:2026-10-24"
    assert event.status == EventStatus.OPEN.value


async def test_автосоздание_уведомляет_служебный_чат(app: App) -> None:
    await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert app.notifier.service == ["Создана встреча на суббота, 24 октября, 17:00."]


async def test_автосоздание_берёт_параметры_из_настроек(app: App) -> None:
    settings = app.services.settings
    await settings.set("capacity", 15, ADMIN_ID)
    await settings.set("default_place", "Кафе", ADMIN_ID)
    await settings.set("default_place_url", "https://maps.example/x", ADMIN_ID)
    await settings.set("default_description", "<b>Привет</b>", ADMIN_ID)
    await settings.set("default_photo_file_id", "AgAC1", ADMIN_ID)
    result = await app.services.lifecycle.create_weekly_event(settings.rules_now(), app.clock.now())
    assert result.event_id is not None
    event = await get_event(app, result.event_id)
    assert (event.capacity, event.place, event.place_url) == (15, "Кафе", "https://maps.example/x")
    assert (event.description_html, event.photo_file_id) == ("<b>Привет</b>", "AgAC1")


async def test_повторное_автосоздание_не_создаёт_дубль(app: App) -> None:  # кейс 15
    rules = app.services.settings.rules_now()
    first = await app.services.lifecycle.create_weekly_event(rules, app.clock.now())
    second = await app.services.lifecycle.create_weekly_event(rules, app.clock.now())
    assert first.status is AutoCreateStatus.CREATED
    assert (second.status, second.recurrence_key) == (
        AutoCreateStatus.ALREADY_EXISTS,
        "weekly:2026-10-24",
    )
    assert second.event_id is None
    assert await count_events(app) == 1
    assert len(app.notifier.service) == 1


async def test_параллельное_автосоздание_даёт_ровно_одну_встречу(app: App) -> None:  # кейс 15
    rules = app.services.settings.rules_now()
    now = app.clock.now()
    results = await asyncio.gather(
        *(app.services.lifecycle.create_weekly_event(rules, now) for _ in range(5))
    )
    statuses = [r.status for r in results]
    assert statuses.count(AutoCreateStatus.CREATED) == 1
    assert statuses.count(AutoCreateStatus.ALREADY_EXISTS) == 4
    assert await count_events(app) == 1


async def test_автосоздание_через_новый_экземпляр_ловит_уникальный_индекс(app: App) -> None:
    # Другой экземпляр сервиса не видит блокировок первого: защищает уникальный recurrence_key
    rules = app.services.settings.rules_now()
    other = LifecycleService(
        app.sessions,
        app.clock,
        EventLocks(),
        app.notifier,
        app.refresher,
        schedule=app.services.settings.schedule_now,
    )
    results = await asyncio.gather(
        app.services.lifecycle.create_weekly_event(rules, app.clock.now()),
        other.create_weekly_event(rules, app.clock.now()),
    )
    assert sorted(r.status.value for r in results) == ["already_exists", "created"]
    assert await count_events(app) == 1


async def test_автосоздание_выключено(app: App) -> None:
    await app.services.settings.set("auto_create_enabled", False, ADMIN_ID)
    result = await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert result.status is AutoCreateStatus.DISABLED
    assert await count_events(app) == 0


async def test_автосоздание_после_встречи_не_создаёт_задним_числом(app: App) -> None:
    # Суббота 18:00 по Монтевидео: целевая суббота 17:00 уже прошла
    app.clock.advance(timedelta(hours=33))
    result = await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert result.status is AutoCreateStatus.SKIPPED_PAST
    assert await count_events(app) == 0


async def test_автосоздание_до_момента_автосоздания_цель_прошлой_недели_в_прошлом(
    app: App,
) -> None:
    app.clock.advance(timedelta(days=2))  # воскресенье 09:00 по Монтевидео, до 10:00
    result = await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert result.status is AutoCreateStatus.SKIPPED_PAST


async def test_автосоздание_в_момент_автосоздания_создаёт_встречу_на_следующую_субботу(
    app: App,
) -> None:
    app.clock.advance(timedelta(days=2, hours=1))  # воскресенье 10:00 по Монтевидео
    result = await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert (result.status, result.recurrence_key) == (AutoCreateStatus.CREATED, "weekly:2026-10-31")


async def test_ключ_считается_по_локальной_дате_а_не_по_UTC(app: App) -> None:
    # Суббота 22:00 по Монтевидео = воскресенье 01:00 UTC
    await app.services.settings.set("event_time", "22:00", ADMIN_ID)
    result = await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert result.status is AutoCreateStatus.CREATED
    assert result.recurrence_key == "weekly:2026-10-24"
    assert result.event_id is not None
    assert (await get_event(app, result.event_id)).starts_at == datetime(
        2026, 10, 25, 1, 0, tzinfo=UTC
    )


async def test_автосоздание_с_недопустимыми_моментами_даёт_INVALID(
    app: App, caplog: pytest.LogCaptureFixture
) -> None:
    # Дедлайн отмены за 7 дней до встречи уже в прошлом: порядок моментов нарушен
    await app.services.settings.set("cancel_deadline_days_before", 7, ADMIN_ID)
    rules = app.services.settings.rules_now()
    with caplog.at_level(logging.WARNING):
        result = await app.services.lifecycle.create_weekly_event(rules, app.clock.now())
    assert (result.status, result.recurrence_key) == (AutoCreateStatus.INVALID, "weekly:2026-10-24")
    assert await count_events(app) == 0
    assert app.notifier.service == []


async def test_недопустимые_настройки_предупреждают_один_раз_на_ключ(
    app: App, caplog: pytest.LogCaptureFixture
) -> None:
    await app.services.settings.set("cancel_deadline_days_before", 7, ADMIN_ID)
    rules = app.services.settings.rules_now()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            await app.services.lifecycle.create_weekly_event(rules, app.clock.now())
    assert caplog.text.count("автосоздание weekly:2026-10-24 невозможно") == 1


async def test_после_исправления_настроек_автосоздание_работает(app: App) -> None:
    await app.services.settings.set("cancel_deadline_days_before", 7, ADMIN_ID)
    rules = app.services.settings.rules_now()
    assert (
        await app.services.lifecycle.create_weekly_event(rules, app.clock.now())
    ).status is AutoCreateStatus.INVALID
    await app.services.settings.reset("cancel_deadline_days_before", ADMIN_ID)
    result = await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert result.status is AutoCreateStatus.CREATED


async def test_автосоздание_без_провайдеров_использует_умолчания(
    app: App, clock: FakeClock
) -> None:
    service = LifecycleService(app.sessions, clock, EventLocks(), app.notifier, app.refresher)
    result = await service.create_weekly_event(RulesSettings(), clock.now())
    assert result.status is AutoCreateStatus.CREATED
    assert result.event_id is not None
    event = await get_event(app, result.event_id)
    assert event.capacity == EventDefaults().capacity


async def test_автосоздание_принимает_асинхронный_провайдер_умолчаний(
    app: App, clock: FakeClock
) -> None:
    async def defaults() -> EventDefaults:
        return EventDefaults(capacity=42, place="Парк")

    service = LifecycleService(
        app.sessions, clock, EventLocks(), app.notifier, app.refresher, defaults_provider=defaults
    )
    result = await service.create_weekly_event(RulesSettings(), clock.now())
    assert result.event_id is not None
    event = await get_event(app, result.event_id)
    assert (event.capacity, event.place) == (42, "Парк")


async def test_автосоздание_сбой_служебного_чата_не_отменяет_создание(
    app: App, clock: FakeClock
) -> None:
    service = LifecycleService(app.sessions, clock, EventLocks(), FailingNotifier(), app.refresher)
    result = await service.create_weekly_event(RulesSettings(), clock.now())
    assert result.status is AutoCreateStatus.CREATED
    assert await count_events(app) == 1


async def test_автосозданная_встреча_пишет_аудит_создания(app: App) -> None:
    result = await app.services.lifecycle.create_weekly_event(
        app.services.settings.rules_now(), app.clock.now()
    )
    assert "event.create" in await audit_actions(app, result.event_id)
