"""Папка журнала на рабочем столе (решение R6).

Рабочий стол ищется через известную папку Windows (FOLDERID_Desktop): её могут перенести,
например, в OneDrive, и тогда `~/Desktop` указывал бы не туда.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

JOURNAL_FOLDER = "rcbot-journal"
FOLDERID_DESKTOP = uuid.UUID("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}")


def desktop_dir() -> Path:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = (
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            )

        guid = GUID.from_buffer_copy(FOLDERID_DESKTOP.bytes_le)
        path_ptr = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)) == 0:
            try:
                if path_ptr.value:
                    return Path(path_ptr.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
    return Path.home() / "Desktop"


def journal_dir(configured: str) -> Path:
    return Path(configured).expanduser() if configured else desktop_dir() / JOURNAL_FOLDER
