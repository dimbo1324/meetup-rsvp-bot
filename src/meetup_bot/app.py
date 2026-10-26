"""Запуск приложения: сборка Bot/Dispatcher, сервисов и планировщика, long polling."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram_dialog import setup_dialogs

from meetup_bot import __version__
from meetup_bot.clock import Clock, ShiftableClock, SystemClock
from meetup_bot.config import Settings
from meetup_bot.container import build_services
from meetup_bot.db.session import create_engine, create_session_factory
from meetup_bot.handlers import ALLOWED_UPDATES, build_router
from meetup_bot.middlewares.rate_limit import install_rate_limit
from meetup_bot.repositories import admins as admins_repo
from meetup_bot.repositories import event_posts
from meetup_bot.scheduler.backup import BACKUPS_DIRNAME
from meetup_bot.scheduler.tick import HEARTBEAT_FILENAME
from meetup_bot.services.ports import PostRefresher
from meetup_bot.services.settings import SettingsService
from meetup_bot.telegram.attendance_ui import AttendanceUI
from meetup_bot.telegram.commands import sync_commands
from meetup_bot.telegram.notifier import TelegramNotifier
from meetup_bot.telegram.post_updater import PostUpdater
from meetup_bot.telegram.service_chat import check_service_chat
from meetup_bot.texts.service import BOT_STARTED_TEXT, DEV_MODE_MARK, SCHEDULER_STOPPED_TEXT

logger = logging.getLogger(__name__)


# Токен бота входит в URL запросов Bot API (…/bot<id>:<токен>/метод) и попадает в трейсбеки
_TOKEN_RE = re.compile(r"bot\d+:[\w-]+")


def _scrub(value: Any) -> Any:
    """Вырезает токены бота из любых строк события перед отправкой в Sentry."""
    if isinstance(value, str):
        return _TOKEN_RE.sub("bot<скрыто>", value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _sentry_before_send(event: Any, _hint: Any) -> Any:
    return _scrub(event)


def _sentry_before_breadcrumb(crumb: Any, _hint: Any) -> Any:
    return _scrub(crumb)


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("бэкапы поддерживаются только для SQLite (sqlite+aiosqlite:///...)")
    return Path(database_url.removeprefix(prefix))


def _init_sentry(dsn: str) -> None:
    if not dsn:
        return
    import sentry_sdk

    # Локальные переменные фреймов содержат URL с токеном, а имена и ID пользователей — ПД
    sentry_sdk.init(
        dsn=dsn,
        send_default_pii=False,
        include_local_variables=False,
        release=__version__,
        before_send=_sentry_before_send,
        before_breadcrumb=_sentry_before_breadcrumb,
    )


def startup_text(config: Settings) -> str:
    """Сообщение о запуске; в режиме dev помечено, чтобы его не спутали с боевым."""
    version = html.escape(config.app_version, quote=False)
    text = BOT_STARTED_TEXT.format(version=version)
    return f"{DEV_MODE_MARK}\n{text}" if config.is_dev else text


def _watch_scheduler(task: asyncio.Task[None], notifier: TelegramNotifier) -> None:
    """Падение планировщика не должно проходить незамеченным."""

    def _done(finished: asyncio.Task[None]) -> None:
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:
            logger.error("планировщик остановился", exc_info=error)
            asyncio.get_running_loop().create_task(
                notifier.send_service(SCHEDULER_STOPPED_TEXT.format(error=type(error).__name__))
            )

    task.add_done_callback(_done)


async def run(config: Settings) -> None:
    _init_sentry(config.sentry_dsn.get_secret_value())
    db_path = _sqlite_path(config.database_url)
    engine = create_engine(config.database_url)
    sessions = create_session_factory(engine)

    # Сдвиг времени нужен только для dev-команды /dev_shift; в бою часы системные
    clock: Clock = ShiftableClock(SystemClock()) if config.is_dev else SystemClock()

    bot = Bot(
        token=config.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    scheduler_task: asyncio.Task[None] | None = None
    post_refresher: PostRefresher | None = None
    try:
        notifier = TelegramNotifier(
            bot, sessions, clock, config.service_chat_id, owner_ids=config.owner_ids
        )

        def make_updater(settings: SettingsService) -> PostRefresher:
            return PostUpdater(bot, sessions, clock, settings)

        services = await build_services(
            sessions,
            clock,
            db_path=db_path,
            backups_dir=db_path.parent / BACKUPS_DIRNAME,
            heartbeat_path=db_path.parent / HEARTBEAT_FILENAME,
            notifier=notifier,
            post_refresher_factory=make_updater,
            send_backup=notifier.send_service_document,
        )
        post_refresher = services.post_refresher
        attendance_ui = AttendanceUI(bot, sessions, clock, services)
        notifier.attach_attendance_ui(attendance_ui)
        await services.admins.sync_owners(config.owner_ids)

        me = await bot.get_me()
        dispatcher = Dispatcher()
        dispatcher.workflow_data.update(
            services=services,
            sessions=sessions,
            post_updater=services.post_refresher,
            bot_username=me.username or "",
            app_config=config,
            clock=clock,
            notifier=notifier,
            attendance_ui=attendance_ui,
        )
        install_rate_limit(dispatcher, clock)
        dispatcher.include_router(build_router())
        setup_dialogs(dispatcher)
        async with sessions() as session:
            admin_ids = await admins_repo.list_admin_ids(session)
        await sync_commands(bot, admin_ids)

        # Очередь пересборки живёт в памяти: после перезапуска догоняем устаревшие посты
        async with sessions() as session:
            for event_id in await event_posts.list_live_event_ids(session):
                services.post_refresher.schedule(event_id)

        if config.is_dev:
            logger.warning("%s: запуск в режиме разработки, dev-команды включены", DEV_MODE_MARK)
        await check_service_chat(bot, notifier, config.service_chat_id)
        await notifier.send_service(startup_text(config))

        scheduler_task = asyncio.create_task(services.scheduler.run_forever(), name="scheduler")
        _watch_scheduler(scheduler_task, notifier)
        # Вебхук (если остался от прежнего запуска) блокирует getUpdates; очередь не сбрасываем,
        # чтобы после простоя обработать накопившиеся нажатия
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await scheduler_task
        closer = getattr(post_refresher, "aclose", None)
        if closer is not None:
            await closer()
        await bot.session.close()
        await engine.dispose()
        logger.info("приложение остановлено")
