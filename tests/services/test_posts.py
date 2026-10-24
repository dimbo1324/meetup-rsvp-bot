import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import FakeClock
from meetup_bot.renderer.post import render_post
from meetup_bot.services.events import EventService
from meetup_bot.services.posts import load_post_data
from meetup_bot.services.registration import RegistrationService, UserInfo

ROOT = Path(__file__).resolve().parent.parent.parent


async def test_пост_собирается_из_данных_бд(
    sessions: async_sessionmaker[AsyncSession], clock: FakeClock, event_service: EventService
) -> None:
    event_id = await event_service.create_event(capacity=3, description_html="Привет", place="Кафе")
    service = RegistrationService(sessions, clock)
    await service.register(event_id, UserInfo(1, "Света", "Иванова", "sveta"))
    await service.register(event_id, UserInfo(2, "", None, "nick"))
    await service.cancel(event_id, 2)

    data = await load_post_data(sessions, event_id)
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
