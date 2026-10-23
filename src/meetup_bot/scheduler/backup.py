"""Ежедневный бэкап SQLite (раздел 17.5)."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from meetup_bot.services.admin_support import Provided, resolve
from meetup_bot.services.timing import DEFAULT_TIMEZONE

logger = logging.getLogger(__name__)

RETRY_AFTER_FAILURE = timedelta(minutes=15)
BACKUP_GLOB = "bot-????-??-??.db"

SendDocument = Callable[[Path], Awaitable[None]]


def backup_path_for(backups_dir: Path, now: datetime, timezone: str = DEFAULT_TIMEZONE) -> Path:
    local_date = now.astimezone(ZoneInfo(timezone)).date()
    return backups_dir / f"bot-{local_date.isoformat()}.db"


def _integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    return bool(row) and row[0] == "ok"


def _backup_sync(db_path: Path, target: Path, backups_dir: Path, keep: int) -> bool:
    if target.exists():
        return False
    if not db_path.exists():
        raise FileNotFoundError(f"база данных не найдена: {db_path}")
    backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Остатки прерванных прошлых запусков не должны копиться и занимать место
    for orphan in backups_dir.glob("*.db.tmp"):
        orphan.unlink(missing_ok=True)
    # Пишем во временный файл и переименовываем: готовый файл бэкапа не бывает полузаписанным,
    # поэтому его существование служит маркером «сегодня уже сделан»
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(db_path)
    try:
        destination = sqlite3.connect(tmp)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    if not _integrity_ok(tmp):
        # Битую копию не оставляем: иначе она стала бы маркером «бэкап за сегодня есть»
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"копия БД не прошла проверку целостности: {target.name}")
    # Бэкап содержит персональные данные: права ограничиваем до публикации под финальным именем
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    for old in sorted(backups_dir.glob(BACKUP_GLOB))[:-keep]:
        old.unlink(missing_ok=True)
    return True


async def backup_database(
    db_path: Path,
    backups_dir: Path,
    now: datetime,
    keep: int = 14,
    *,
    timezone: str = DEFAULT_TIMEZONE,
) -> Path | None:
    """Делает копию БД; возвращает путь или None, если копия за эту локальную дату уже есть."""
    if keep < 1:
        raise ValueError("keep должен быть >= 1")
    target = backup_path_for(backups_dir, now, timezone)
    if not await asyncio.to_thread(_backup_sync, db_path, target, backups_dir, keep):
        return None
    logger.info("бэкап БД создан: %s", target)
    return target


class BackupTask:
    """Задача планировщика: бэкап в 04:00 локального времени (или при первом тике после)."""

    name = "backup"

    def __init__(
        self,
        db_path: Path,
        backups_dir: Path,
        *,
        timezone: Provided[str] = DEFAULT_TIMEZONE,
        at: time = time(4, 0),
        keep: int = 14,
        send_document: SendDocument | None = None,
    ) -> None:
        self._db_path = db_path
        self._backups_dir = backups_dir
        self._timezone = timezone
        self._at = at
        self._keep = keep
        self._send_document = send_document
        self._retry_at: datetime | None = None

    async def run(self, now: datetime) -> int:
        # При постоянной ошибке (нет места, права) не долбим диск каждую минуту
        if self._retry_at is not None and now < self._retry_at:
            return 0
        timezone = resolve(self._timezone)
        local = now.astimezone(ZoneInfo(timezone))
        if local.time() < self._at:
            return 0
        try:
            path = await backup_database(
                self._db_path, self._backups_dir, now, self._keep, timezone=timezone
            )
        except Exception:
            self._retry_at = now + RETRY_AFTER_FAILURE
            raise
        self._retry_at = None
        if path is None:
            return 0
        if self._send_document is not None:
            try:
                await self._send_document(path)
            except Exception:
                # Локальная копия уже есть; повторять отправку каждую минуту не нужно
                logger.exception("копия бэкапа не отправлена в служебный чат")
        return 1
