"""Корень сборки: создаёт объекты и связывает их через конструкторы, без глобальных синглтонов."""

from __future__ import annotations

import asyncio
import atexit
import logging
from dataclasses import dataclass, field
from types import TracebackType
from urllib.parse import urlparse

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from rcbot import __version__
from rcbot.application.ports import Journal, NullJournal
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretStore
from rcbot.config.settings import SAE_HOST, Settings, load_settings
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
from rcbot.infrastructure.gubuy.sae_source import SaeSource
from rcbot.infrastructure.http.client import SafeHttp
from rcbot.infrastructure.journal.desktop import journal_dir
from rcbot.infrastructure.journal.files import FileJournal
from rcbot.infrastructure.logging.redaction import REDACTOR
from rcbot.infrastructure.logging.setup import setup_logging
from rcbot.infrastructure.sentry import init_sentry

logger = logging.getLogger(__name__)

TARGET_NAME = "registro-civil"
BYTES_PER_GB = 2**30


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

    def __enter__(self) -> Core:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def build_core(
    paths: Paths | None = None, *, console: bool = True, level: str = "INFO", command: str = ""
) -> Core:
    paths = paths or Paths.default()
    paths.ensure()
    paths.apply_process_env()
    setup_logging(paths.log_file, level=level, console=console)
    settings = load_settings(paths.env_file)
    private = (settings.tramite_id, _etapa(settings.agenda_url), _ping_key(settings.watchdog_url))
    REDACTOR.register_many(v for v in private if v)
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
            max_bytes=int(s.journal_max_gb * BYTES_PER_GB),
            trim_to_bytes=int(s.journal_trim_to_gb * BYTES_PER_GB),
            # Запись на диск в отдельном потоке: зависший диск не должен тормозить тревоги
            background=s.journal_background,
        )
    except OSError as error:
        logger.warning("журнал наблюдения недоступен: %s", type(error).__name__)
        return NullJournal()
    journal.attach_log_handler()
    if s.journal_background:
        # Очередь сбрасывается при обычном завершении процесса: последние записи команды не теряются
        atexit.register(journal.close)
    return journal


def _etapa(agenda_url: str) -> str:
    # Номер этапа — часть адреса заявки: в логах его быть не должно (решение R1)
    parts = [p for p in agenda_url.split("/") if p.isdigit()]
    return parts[0] if parts else ""


def _ping_key(watchdog_url: str) -> str:
    # Путь адреса сторожа — ключ пинга (UUID healthchecks.io): с ним любой подделает «жив»
    return urlparse(watchdog_url).path.strip("/") if watchdog_url else ""


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
        targets=SqlTargetRepository(sessions),
        incidents=SqlIncidentRepository(sessions),
        stats=SqlStatsRepository(sessions),
        events=SqlEventLog(sessions),
        kv=SqlKeyValueStore(sessions),
        maintenance=SqlMaintenance(engine, sessions),
    )


def build_source(core: Core) -> SaeSource:
    """Единственный источник — публичная страница записи SAE без входа (решения R12, R13)."""
    s = core.settings
    http = SafeHttp([SAE_HOST], timeout_sec=s.request_timeout_sec)
    return SaeSource(http, s.sae_url, core.paths.shots, core.clock)


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
