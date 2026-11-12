"""Очистка по срокам хранения SPEC 2.7: при старте и каждые 6 часов, раз в неделю — VACUUM."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from rcbot.application.ports import (
    Clock,
    FileStore,
    Journal,
    KeyValueStore,
    Maintenance,
    NullJournal,
)

logger = logging.getLogger(__name__)

EVERY = timedelta(hours=6)
COMPACT_EVERY = timedelta(days=7)
K_LAST_COMPACT = "last_compact_at"

DB_RETENTION = {
    "incidents": timedelta(days=180),
    "poll_stats_hourly": timedelta(days=30),
    "app_events": timedelta(days=30),
}
FILE_RETENTION: dict[str, tuple[timedelta, int | None]] = {
    "discovery": (timedelta(days=7), None),
    "quarantine": (timedelta(days=7), 20),
    "shots": (timedelta(days=3), 20),
}
LOG_RETENTION = timedelta(days=14)
# Квота журнала проверяется часто: за 10 минут журнал вырасти на гигабайты не успеет
JOURNAL_QUOTA_EVERY_SEC = 600.0


class Housekeeping:
    def __init__(
        self,
        maintenance: Maintenance,
        files: FileStore,
        kv: KeyValueStore,
        clock: Clock,
        journal: Journal | None = None,
    ) -> None:
        self._maintenance = maintenance
        self._files = files
        self._kv = kv
        self._clock = clock
        self._journal: Journal = journal or NullJournal()

    def start(self) -> int:
        """Временные файлы очищаются при каждом запуске."""
        return self._files.clear("tmp")

    async def run_once(self) -> dict[str, int]:
        now = self._clock.now()
        removed = await self._maintenance.delete_older_than(
            {table: now - keep for table, keep in DB_RETENTION.items()}
        )
        for name, (max_age, max_files) in FILE_RETENTION.items():
            removed[name] = await asyncio.to_thread(
                self._files.prune, name, max_age, now, max_files
            )
        removed["logs"] = await asyncio.to_thread(self._files.prune_logs, LOG_RETENTION, now)
        if await self._compact_due(now):
            await self._maintenance.compact()
            await self._kv.set(K_LAST_COMPACT, now.isoformat())
            logger.info("БД уплотнена (wal_checkpoint + VACUUM)")
        removed = {k: v for k, v in removed.items() if v}
        if removed:
            logger.info("очистка: %s", removed)
            self._journal.event("housekeeping", f"очистка по срокам: {removed}")
        return removed

    async def _compact_due(self, now: datetime) -> bool:
        last = await self._kv.get(K_LAST_COMPACT)
        return last is None or now - datetime.fromisoformat(last) >= COMPACT_EVERY

    def folder_size(self) -> int:
        return self._files.folder_size()

    async def journal_quota_once(self) -> tuple[int, int]:
        files, freed = await asyncio.to_thread(self._journal.enforce_quota)
        if files:
            message = f"квота журнала: удалено {files} старых файлов, {freed / 2**30:.2f} ГБ"
            logger.warning(message)
            self._journal.event("journal.quota", message, "WARNING")
        return files, freed

    async def journal_quota_forever(self) -> None:
        while True:
            await self.journal_quota_once()
            await self._clock.sleep(JOURNAL_QUOTA_EVERY_SEC)

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await self._clock.sleep(EVERY.total_seconds())
