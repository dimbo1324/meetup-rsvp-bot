"""Общая тестовая инфраструктура: записывающие порты и сборка приложения на фейковых часах."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meetup_bot.clock import FakeClock
from meetup_bot.container import Services, build_services
from meetup_bot.db.base import Base
from meetup_bot.db.session import create_engine, create_session_factory
from meetup_bot.services.safe import BackgroundTasks
from meetup_bot.user_info import UserInfo

OWNER_ID = 1
ADMIN_ID = 2


class RecordingNotifier:
    """Порт `Notifier`, который запоминает все отправки; `blocked` — кому писать нельзя."""

    def __init__(self) -> None:
        self.dms: list[tuple[int, str]] = []
        self.service: list[str] = []
        self.prompts: list[tuple[int, int]] = []
        self.blocked: set[int] = set()

    async def send_dm(self, user_id: int, text: str) -> bool:
        if user_id in self.blocked:
            return False
        self.dms.append((user_id, text))
        return True

    async def send_service(self, text: str) -> bool:
        self.service.append(text)
        return True

    async def send_attendance_prompt(self, admin_id: int, event_id: int) -> bool:
        self.prompts.append((admin_id, event_id))
        return True

    def dms_to(self, user_id: int) -> list[str]:
        return [text for uid, text in self.dms if uid == user_id]


class RecordingRefresher:
    """Порт `PostRefresher`: какие встречи просили пересобрать."""

    def __init__(self) -> None:
        self.scheduled: list[int] = []

    def schedule(self, event_id: int) -> None:
        self.scheduled.append(event_id)


class InlineBackgroundTasks(BackgroundTasks):
    """Фоновые задачи стартуют сразу, до первого настоящего ожидания.

    Записывающие порты не ждут сети, поэтому уведомление успевает записаться ещё внутри
    `spawn`, и тестам не нужен `drain()` после каждого `register()`. Тесты самой отложенной
    отправки берут обычный `BackgroundTasks`.
    """

    def spawn(self, effect: Coroutine[Any, Any, object]) -> None:
        task = asyncio.Task(self._guarded(effect), eager_start=True)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


@dataclass
class App:
    services: Services
    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine
    clock: FakeClock
    notifier: RecordingNotifier
    refresher: RecordingRefresher
    tmp_path: Path
    extra: dict[str, object] = field(default_factory=dict)


def person(n: int) -> UserInfo:
    """Участник с id 1000+n."""
    return UserInfo(id=1000 + n, first_name=f"Участник{n}", username=f"user{n}")


async def make_app(tmp_path: Path, clock: FakeClock, **build_kwargs: object) -> App:
    """Приложение на файловой SQLite: владелец (id=1) и админ (id=2) уже созданы."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    notifier = RecordingNotifier()
    refresher = RecordingRefresher()
    build_kwargs.setdefault("background", InlineBackgroundTasks())
    services = await build_services(
        sessions,
        clock,
        db_path=tmp_path / "app.db",
        backups_dir=tmp_path / "backups",
        heartbeat_path=tmp_path / "heartbeat",
        notifier=notifier,
        post_refresher=refresher,
        **build_kwargs,  # type: ignore[arg-type]
    )
    await services.admins.sync_owners([OWNER_ID])
    await services.admins.add_admin(OWNER_ID, UserInfo(ADMIN_ID, "Админ", None, "admin2"))
    return App(services, sessions, engine, clock, notifier, refresher, tmp_path)
