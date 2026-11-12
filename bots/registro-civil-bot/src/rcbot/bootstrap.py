"""Корень сборки: создаёт объекты и связывает их через конструкторы, без глобальных синглтонов."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rcbot import __version__
from rcbot.application.ports import Journal, NullJournal
from rcbot.application.session_manager import SessionManager
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretStore
from rcbot.config.settings import Settings, load_settings
from rcbot.domain.models import WatchTarget
from rcbot.infrastructure.clock import SystemClock
from rcbot.infrastructure.db.engine import create_engine, create_session_factory
from rcbot.infrastructure.db.repositories import (
    SqlEventLog,
    SqlIncidentRepository,
    SqlKeyValueStore,
    SqlMaintenance,
    SqlStatsRepository,
    SqlTargetRepository,
)
from rcbot.infrastructure.gubuy.api_source import ApiSource
from rcbot.infrastructure.gubuy.auth import PlaywrightAuthenticator
from rcbot.infrastructure.gubuy.browser import BrowserOptions
from rcbot.infrastructure.gubuy.dom_source import DomSource
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.journal.desktop import journal_dir
from rcbot.infrastructure.journal.files import FileJournal
from rcbot.infrastructure.logging.redaction import REDACTOR
from rcbot.infrastructure.logging.setup import setup_logging
from rcbot.infrastructure.sentry import init_sentry

logger = logging.getLogger(__name__)

TARGET_NAME = "registro-civil"


@dataclass
class Core:
    """Конфигурация и хранилище: общее для всех команд CLI."""

    paths: Paths
    settings: Settings
    secrets: SecretStore
    clock: SystemClock = field(default_factory=SystemClock)
    journal: Journal = field(default_factory=NullJournal)

    def close(self) -> None:
        if isinstance(self.journal, FileJournal):
            self.journal.close()


def build_core(
    paths: Paths | None = None, *, console: bool = True, level: str = "INFO", command: str = ""
) -> Core:
    paths = paths or Paths.default()
    paths.ensure()
    paths.apply_process_env()
    setup_logging(paths.log_file, level=level, console=console)
    settings = load_settings(paths.env_file)
    REDACTOR.register_many(v for v in (settings.tramite_id, _etapa(settings.agenda_url)) if v)
    secrets = SecretStore(on_read=REDACTOR.register)
    init_sentry(settings.sentry_enabled, settings.sentry_dsn, f"rcbot@{__version__}")
    core = Core(paths, settings, secrets)
    core.journal = build_journal(core)
    if command:
        core.journal.event(f"cli.{command}", f"запущена команда rcbot {command}")
    return core


def build_journal(core: Core) -> Journal:
    """Журнал наблюдения на рабочем столе (R6); если папка недоступна — бот работает без него."""
    s = core.settings
    if not s.journal_enabled:
        return NullJournal()
    try:
        journal = FileJournal(
            journal_dir(s.journal_dir),
            s.tz,
            core.clock,
            max_bytes=int(s.journal_max_gb * 2**30),
            trim_to_bytes=int(s.journal_trim_to_gb * 2**30),
        )
    except OSError as error:
        logger.warning("журнал наблюдения недоступен: %s", type(error).__name__)
        return NullJournal()
    journal.attach_log_handler()
    return journal


def _etapa(agenda_url: str) -> str:
    # Номер этапа — часть адреса заявки: в логах его быть не должно (решение R1)
    parts = [p for p in agenda_url.split("/") if p.isdigit()]
    return parts[0] if parts else ""


def upgrade_database(paths: Paths) -> None:
    config = Config(str(paths.alembic_ini))
    config.attributes["database_url"] = paths.database_url()
    config.attributes["skip_logging"] = True
    command.upgrade(config, "head")


async def upgrade_database_async(paths: Paths) -> None:
    # env.py миграций запускает свой цикл событий: из работающего цикла — только в потоке
    await asyncio.to_thread(upgrade_database, paths)


@dataclass
class Storage:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    targets: SqlTargetRepository
    incidents: SqlIncidentRepository
    stats: SqlStatsRepository
    events: SqlEventLog
    kv: SqlKeyValueStore
    maintenance: SqlMaintenance

    async def close(self) -> None:
        await self.engine.dispose()


async def open_storage(core: Core) -> Storage:
    await upgrade_database_async(core.paths)
    engine = create_engine(core.paths.database_url())
    sessions = create_session_factory(engine)
    return Storage(
        engine=engine,
        sessions=sessions,
        targets=SqlTargetRepository(sessions),
        incidents=SqlIncidentRepository(sessions),
        stats=SqlStatsRepository(sessions),
        events=SqlEventLog(sessions),
        kv=SqlKeyValueStore(sessions),
        maintenance=SqlMaintenance(engine, sessions),
    )


def browser_options(
    core: Core, *, headless: bool | None = None, channel: str = ""
) -> BrowserOptions:
    return BrowserOptions(
        headless=core.settings.headless if headless is None else headless, channel=channel
    )


def build_authenticator(core: Core) -> PlaywrightAuthenticator:
    s = core.settings
    return PlaywrightAuthenticator(
        login_url=s.login_url,
        doc_country=s.doc_country,
        doc_type=s.doc_type,
        credentials=core.secrets.credentials,
        options=browser_options(core),
    )


def build_api_source(core: Core) -> ApiSource:
    s = core.settings
    http = SafeHttp(s.allowed_hosts(), timeout_sec=s.request_timeout_sec)
    return ApiSource(http, s.api_url, s.agenda_url, core.paths.quarantine, core.clock)


def build_dom_source(core: Core) -> DomSource:
    s = core.settings
    return DomSource(
        s.agenda_url, s.location_text, browser_options(core), core.paths.shots, core.clock
    )


def build_session(core: Core) -> SessionManager:
    return SessionManager(build_authenticator(core), core.clock, core.settings.relogin_max_per_hour)


def transient_target(core: Core) -> WatchTarget:
    s = core.settings
    return WatchTarget(0, TARGET_NAME, s.agenda_url, s.location_text, s.months_ahead)


async def ensure_target(
    core: Core, storage: Storage, months_ahead: int | None = None
) -> WatchTarget:
    s = core.settings
    return await storage.targets.ensure(
        TARGET_NAME, s.agenda_url, s.location_text, months_ahead or s.months_ahead
    )
