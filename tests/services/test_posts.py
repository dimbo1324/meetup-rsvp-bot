import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import FakeClock
from meetup_bot.db.models import Event
from meetup_bot.renderer.post import render_post
from meetup_bot.repositories import registrations
from meetup_bot.services.events import EventService
from meetup_bot.services.posts import load_post_data
from meetup_bot.services.registration import RegisterStatus, RegistrationService, UserInfo
from meetup_bot.texts.render import TIME_RANGE_DASH
from tests.helpers import ADMIN_ID, OWNER_ID, App, person

ROOT = Path(__file__).resolve().parent.parent.parent


async def test_пост_собирается_из_данных_бд(
    sessions: async_sessionmaker[AsyncSession], clock: FakeClock, event_service: EventService
) -> None:
    event_id = await event_service.create_event(capacity=3, description_html="Привет", place="Кафе")
    service = RegistrationService(sessions, clock)
    await service.register(event_id, UserInfo(1, "Света", "Иванова", "sveta"))
    await service.register(event_id, UserInfo(2, "", None, "nick"))
    await service.cancel(event_id, 2)

    data = await load_post_data(sessions, event_id, show_names=True)
    assert data is not None
    assert data.seats_taken == 1
    assert [p.name for p in data.participants] == ["Света Иванова"]
    post = render_post(data)
    assert "Записались: 1 из 3" in post.text
    assert "1. Света Иванова" in post.text


async def test_несуществующая_встреча(sessions: async_sessionmaker[AsyncSession]) -> None:
    assert await load_post_data(sessions, 404) is None


def test_миграции_совпадают_с_моделями(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'm.db'}",
    }
    run = lambda *args: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert run("upgrade", "head").returncode == 0
    check = run("check")
    assert check.returncode == 0, check.stdout + check.stderr


async def _waiting_app_event(app: App, capacity: int, active: int, queued: int) -> int:
    await app.services.settings.set("waitlist_enabled", True, OWNER_ID)
    event_id = (
        await app.services.events.create_event_as_admin(OWNER_ID, capacity=capacity)
    ).event_id
    for n in range(active + queued):
        await app.services.registration.register(event_id, person(n))
    app.refresher.scheduled.clear()
    return event_id


async def test_load_post_data_считает_очередь(app: App) -> None:
    event_id = await _waiting_app_event(app, 2, 2, 3)
    data = await load_post_data(app.sessions, event_id)
    assert data is not None
    assert (data.seats_taken, data.waiting) == (2, 3)
    assert "В очереди: 3" in render_post(data).text


async def test_load_post_data_пустая_очередь_и_имена_по_умолчанию_скрыты(app: App) -> None:
    event_id = await _waiting_app_event(app, 3, 1, 0)
    data = await load_post_data(app.sessions, event_id)
    assert data is not None
    assert data.waiting == 0
    assert data.show_names is False


async def test_count_waitlist_считает_только_очередь_нужной_встречи(app: App) -> None:
    first = await _waiting_app_event(app, 1, 1, 2)
    second = await _waiting_app_event(app, 1, 1, 1)
    async with app.sessions() as session:
        assert await registrations.count_waitlist(session, first) == 2
        assert await registrations.count_waitlist(session, second) == 1


async def test_постановка_в_очередь_обновляет_пост(app: App) -> None:
    event_id = await _waiting_app_event(app, 1, 1, 0)
    result = await app.services.registration.register(event_id, person(5))
    assert result.status is RegisterStatus.WAITLISTED
    assert app.refresher.scheduled == [event_id]


async def test_выход_из_очереди_обновляет_пост(app: App) -> None:
    event_id = await _waiting_app_event(app, 1, 1, 1)
    await app.services.registration.cancel(event_id, person(1).id)
    assert app.refresher.scheduled == [event_id]


async def test_продвижение_при_отмене_активного_обновляет_пост(app: App) -> None:
    event_id = await _waiting_app_event(app, 1, 1, 1)
    await app.services.registration.cancel(event_id, person(0).id)
    assert app.refresher.scheduled == [event_id]
    data = await load_post_data(app.sessions, event_id)
    assert data is not None
    assert data.waiting == 0


async def test_удаление_админом_активного_и_из_очереди_обновляет_пост(app: App) -> None:
    event_id = await _waiting_app_event(app, 1, 1, 2)
    async with app.sessions() as session:
        queue = await registrations.list_waitlist(session, event_id)
        active = await registrations.list_active_with_users(session, event_id)
    await app.services.participants.admin_remove(ADMIN_ID, event_id, queue[-1].id)
    assert app.refresher.scheduled == [event_id]
    app.refresher.scheduled.clear()
    await app.services.participants.admin_remove(ADMIN_ID, event_id, active[0][0].id)
    assert app.refresher.scheduled == [event_id]


async def test_рост_лимита_продвигает_очередь_и_обновляет_пост(app: App) -> None:
    event_id = await _waiting_app_event(app, 1, 1, 2)
    await app.services.events.set_capacity(OWNER_ID, event_id, 2)
    assert app.refresher.scheduled == [event_id]
    data = await load_post_data(app.sessions, event_id)
    assert data is not None
    assert data.waiting == 1


async def test_load_post_data_передаёт_конец_встречи(app: App) -> None:
    event_id = (await app.services.events.create_event_as_admin(OWNER_ID, capacity=5)).event_id
    async with app.sessions() as session:
        event = await session.get(Event, event_id)
        assert event is not None
        starts_at, ends_at = event.starts_at, event.ends_at
    data = await load_post_data(app.sessions, event_id)
    assert data is not None
    assert (data.starts_at, data.ends_at) == (starts_at, ends_at)
    assert data.ends_at is not None and data.ends_at > data.starts_at
    assert TIME_RANGE_DASH in render_post(data).text.split("\n")[0]
