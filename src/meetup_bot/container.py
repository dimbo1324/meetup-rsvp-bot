"""Корень сборки: единственное место, где сервисы связываются друг с другом.

Здесь принимаются решения, общие для всех сервисов: один реестр блокировок встреч, один
снимок настроек (синхронные провайдеры читают кэш `SettingsService`, а не БД), единые
тексты уведомлений. Telegram-слой (веха M2) передаёт сюда реализации портов
`Notifier` и `PostRefresher`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from meetup_bot.clock import Clock
from meetup_bot.scheduler.backup import BackupTask, SendDocument
from meetup_bot.scheduler.tasks import (
    AttendanceExpireTask,
    AttendancePromptTask,
    AttendanceReminderTask,
    AutoCreateTask,
    BanRecheckTask,
    CloseRegistrationTask,
    FinishEventsTask,
    PostReminderTask,
    ReminderTask,
    ScheduledTask,
)
from meetup_bot.scheduler.tick import Scheduler
from meetup_bot.services.admin_support import RenderText
from meetup_bot.services.admins import AdminService
from meetup_bot.services.attendance import AttendanceService
from meetup_bot.services.attendance_prompts import AttendancePromptService
from meetup_bot.services.bans import BanService
from meetup_bot.services.events import EventService
from meetup_bot.services.export import ExportService
from meetup_bot.services.lifecycle import EventDefaults, LifecycleService
from meetup_bot.services.locks import EventLocks
from meetup_bot.services.participants import ParticipantService
from meetup_bot.services.ports import Notifier, NullNotifier, NullPostRefresher, PostRefresher
from meetup_bot.services.post_reminders import PostReminderService
from meetup_bot.services.posts import PostService
from meetup_bot.services.registration import RegistrationService
from meetup_bot.services.reminders import ReminderService
from meetup_bot.services.safe import BackgroundTasks
from meetup_bot.services.settings import SettingsService
from meetup_bot.services.users import UserService
from meetup_bot.texts.defaults import TEXT_PREFIX


@dataclass(frozen=True)
class Services:
    locks: EventLocks
    settings: SettingsService
    admins: AdminService
    events: EventService
    registration: RegistrationService
    participants: ParticipantService
    bans: BanService
    attendance: AttendanceService
    lifecycle: LifecycleService
    export: ExportService
    scheduler: Scheduler
    post_refresher: PostRefresher
    notifier: Notifier
    users: UserService
    posts: PostService
    # Фоновые уведомления; при остановке приложения вызвать `await background.aclose()`
    background: BackgroundTasks


def make_text_renderer(settings: SettingsService) -> RenderText:
    """Адаптер «ключ уведомления → настраиваемый текст `text.*`» (часовой пояс из настроек)."""

    def render(key: str, params: dict[str, Any]) -> str:
        return settings.render_message(TEXT_PREFIX + key, params)

    return render


async def build_services(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    db_path: Path,
    backups_dir: Path,
    heartbeat_path: Path,
    notifier: Notifier | None = None,
    post_refresher: PostRefresher | None = None,
    post_refresher_factory: Callable[[SettingsService], PostRefresher] | None = None,
    send_backup: SendDocument | None = None,
    background: BackgroundTasks | None = None,
) -> Services:
    """Собирает все сервисы и планировщик. Вызывать один раз при старте приложения."""
    notifier = notifier or NullNotifier()
    locks = EventLocks()
    background = background or BackgroundTasks()

    settings = SettingsService(sessions, clock)
    await settings.load()
    # Обновитель постов читает настройки (имена в посте, «+1»), поэтому создаётся после них
    if post_refresher_factory is not None:
        post_refresher = post_refresher_factory(settings)
    post_refresher = post_refresher or NullPostRefresher()
    render = make_text_renderer(settings)

    def defaults() -> EventDefaults:
        d = settings.defaults_now()
        return EventDefaults(
            capacity=d.capacity,
            description_html=d.description_html,
            place=d.place,
            place_url=d.place_url,
            photo_file_id=d.photo_file_id,
        )

    bans = BanService(
        sessions,
        clock,
        locks,
        notifier,
        post_refresher,
        rules_provider=settings.rules_now,
        render_text=render,
    )
    events = EventService(
        sessions,
        clock,
        settings.schedule_now,
        locks=locks,
        notifier=notifier,
        post_refresher=post_refresher,
        rules=settings.rules_now,
        render_text=render,
        ban_service=bans,
    )
    registration = RegistrationService(
        sessions,
        clock,
        locks=locks,
        notifier=notifier,
        post_refresher=post_refresher,
        rules_provider=settings.rules_now,
        options_provider=settings.options_now,
        render_text=render,
        schedule=settings.schedule_now,
        background=background,
    )
    attendance = AttendanceService(sessions, clock, locks, bans, rules_provider=settings.rules_now)
    participants = ParticipantService(
        sessions,
        clock,
        locks,
        notifier,
        post_refresher,
        settings.rules_now,
        render,
        ban_service=bans,
    )
    lifecycle = LifecycleService(
        sessions,
        clock,
        locks,
        notifier,
        post_refresher,
        events=events,
        schedule=settings.schedule_now,
        defaults_provider=defaults,
    )

    attendance_prompts = AttendancePromptService(sessions, notifier, settings.schedule_now)

    # Порядок важен: сначала создаём и напоминаем, потом закрываем и завершаем, затем явка
    tasks: list[ScheduledTask] = [
        AutoCreateTask(lifecycle, settings.rules_now),
        ReminderTask(ReminderService(sessions, notifier, settings.rules_now, render)),
        PostReminderTask(
            PostReminderService(
                sessions, notifier, settings.rules_now, settings.schedule_now, locks=locks
            )
        ),
        CloseRegistrationTask(lifecycle),
        FinishEventsTask(lifecycle),
        AttendancePromptTask(attendance_prompts),
        AttendanceReminderTask(attendance_prompts),
        AttendanceExpireTask(attendance),
        BanRecheckTask(attendance),
        BackupTask(
            db_path,
            backups_dir,
            timezone=lambda: settings.timezone_now().key,
            send_document=send_backup,
        ),
    ]
    scheduler = Scheduler(clock, tasks, heartbeat_path=heartbeat_path)

    return Services(
        locks=locks,
        settings=settings,
        admins=AdminService(sessions, clock),
        events=events,
        registration=registration,
        participants=participants,
        bans=bans,
        attendance=attendance,
        lifecycle=lifecycle,
        export=ExportService(sessions, clock),
        scheduler=scheduler,
        post_refresher=post_refresher,
        notifier=notifier,
        users=UserService(sessions, clock),
        posts=PostService(sessions, settings),
        background=background,
    )
