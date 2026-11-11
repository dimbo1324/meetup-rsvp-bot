"""Один экземпляр бота (SPEC 2.6): именованный мьютекс Windows и заблокированный lock-файл.

Мьютекс ловит второй запуск мгновенно, lock-файл страхует там, где мьютекса нет (не Windows).
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from types import TracebackType
from typing import IO

ERROR_ALREADY_EXISTS = 183


class AlreadyRunning(RuntimeError):
    """Бот уже запущен."""


class SingleInstance:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        digest = hashlib.sha256(str(lock_path.resolve()).encode()).hexdigest()[:12]
        self._mutex_name = f"Local\\rcbot-{digest}"
        self._mutex: int | None = None
        self._file: IO[bytes] | None = None

    def acquire(self) -> None:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateMutexW(None, False, self._mutex_name)
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                raise AlreadyRunning("rcbot уже запущен (мьютекс занят)")
            self._mutex = int(handle)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._lock_path.open("a+b")
        try:
            _lock_file(lock)
        except OSError:
            lock.close()
            self.release()
            raise AlreadyRunning("rcbot уже запущен (lock-файл занят)") from None
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()).encode())
        lock.flush()
        self._file = lock

    def release(self) -> None:
        if self._file is not None:
            try:
                _unlock_file(self._file)
            finally:
                self._file.close()
                self._file = None
        if self._mutex is not None and sys.platform == "win32":
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(self._mutex)
            self._mutex = None

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(
        self, exc: type[BaseException] | None, value: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


def _lock_file(handle: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
