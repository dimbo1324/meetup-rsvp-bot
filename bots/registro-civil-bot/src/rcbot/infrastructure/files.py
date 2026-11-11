"""Файлы бота на диске (SPEC 2.7): очистка по срокам и количеству, размер папки."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from rcbot.config.paths import Paths

logger = logging.getLogger(__name__)


class FileJanitor:
    def __init__(self, paths: Paths) -> None:
        self._paths = paths
        self._dirs = {
            "discovery": paths.discovery,
            "quarantine": paths.quarantine,
            "shots": paths.shots,
            "tmp": paths.tmp,
        }

    def prune(
        self, name: str, max_age: timedelta, now: datetime, max_files: int | None = None
    ) -> int:
        directory = self._dirs[name]
        if not directory.exists():
            return 0
        files = sorted(
            (p for p in directory.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime
        )
        cutoff = (now - max_age).timestamp()
        doomed = [p for p in files if p.stat().st_mtime < cutoff]
        if max_files is not None:
            survivors = [p for p in files if p not in doomed]
            doomed += survivors[: max(0, len(survivors) - max_files)]
        for path in doomed:
            path.unlink(missing_ok=True)
        return len(doomed)

    def clear(self, name: str) -> int:
        directory = self._dirs[name]
        if not directory.exists():
            return 0
        count = 0
        for path in directory.iterdir():
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            count += 1
        return count

    def prune_logs(self, max_age: timedelta, now: datetime) -> int:
        """Ротированные логи старше срока; текущий файл не трогается."""
        logs = self._paths.logs
        if not logs.exists():
            return 0
        cutoff = (now - max_age).timestamp()
        current = self._paths.log_file.name
        doomed = [
            p for p in logs.glob("rcbot.log*") if p.name != current and p.stat().st_mtime < cutoff
        ]
        for path in doomed:
            path.unlink(missing_ok=True)
        return len(doomed)

    def folder_size(self) -> int:
        total = 0
        for root, _, files in os.walk(self._paths.home):
            if ".git" in Path(root).parts:
                continue
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        return total


def purge(paths: Paths) -> list[Path]:
    """`rcbot purge`: логи, временные данные, карантин, снимки, дампы разведки, кеш байт-кода."""
    removed = []
    for directory in (
        paths.logs,
        paths.tmp,
        paths.quarantine,
        paths.shots,
        paths.discovery,
        paths.cache,
    ):
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(directory)
    return removed
