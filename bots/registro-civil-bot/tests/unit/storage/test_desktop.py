"""Папка журнала на рабочем столе (R6): известная папка Windows, запасной путь, настройка."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

import pytest

from rcbot.infrastructure.journal import desktop
from rcbot.infrastructure.journal.desktop import JOURNAL_FOLDER, desktop_dir, journal_dir


class FakeShell32:
    """Подмена SHGetKnownFolderPath: возвращает код и (при успехе) записывает путь в указатель."""

    def __init__(self, code: int, path: str | None) -> None:
        self.code = code
        self.path = path
        self.calls = 0

    def SHGetKnownFolderPath(self, guid: Any, flags: int, token: Any, out: Any) -> int:
        self.calls += 1
        if self.path is not None:
            out._obj.value = self.path  # byref(c_wchar_p) → сам указатель
        return self.code


class FakeOle32:
    def __init__(self) -> None:
        self.freed = 0

    def CoTaskMemFree(self, pointer: Any) -> None:
        self.freed += 1


class FakeWindll:
    def __init__(self, shell: FakeShell32, ole: FakeOle32) -> None:
        self.shell32 = shell
        self.ole32 = ole


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def install(monkeypatch: pytest.MonkeyPatch, shell: FakeShell32) -> FakeOle32:
    ole = FakeOle32()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", FakeWindll(shell, ole), raising=False)
    return ole


@pytest.mark.skipif(sys.platform != "win32", reason="известная папка Windows")
def test_настоящий_вызов_windows_возвращает_существующий_каталог() -> None:
    path = desktop_dir()
    assert path.is_absolute() and path.is_dir()


def test_успешный_ответ_api_возвращает_путь_и_освобождает_память(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ole = install(monkeypatch, FakeShell32(0, r"D:\OneDrive\Рабочий стол"))
    assert desktop_dir() == Path(r"D:\OneDrive\Рабочий стол")
    assert ole.freed == 1  # CoTaskMemFree обязателен: иначе утечка при каждом вызове


def test_ошибка_api_даёт_запасной_путь_профиль_desktop(
    monkeypatch: pytest.MonkeyPatch, fake_home: Path
) -> None:
    shell = FakeShell32(0x80070057, None)  # E_INVALIDARG
    ole = install(monkeypatch, shell)
    assert desktop_dir() == fake_home / "Desktop"
    assert shell.calls == 1 and ole.freed == 0


def test_пустой_путь_от_api_даёт_запасной_путь_и_освобождает_память(
    monkeypatch: pytest.MonkeyPatch, fake_home: Path
) -> None:
    ole = install(monkeypatch, FakeShell32(0, ""))
    assert desktop_dir() == fake_home / "Desktop"
    assert ole.freed == 1


def test_не_windows_берёт_профиль_desktop_без_вызова_api(
    monkeypatch: pytest.MonkeyPatch, fake_home: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop_dir() == fake_home / "Desktop"


def test_пустая_настройка_даёт_папку_rcbot_journal_на_рабочем_столе(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(desktop, "desktop_dir", lambda: tmp_path / "Desktop")
    assert journal_dir("") == tmp_path / "Desktop" / "rcbot-journal"
    assert JOURNAL_FOLDER == "rcbot-journal"


def test_заданная_настройка_используется_как_есть_и_рабочий_стол_не_опрашивается(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom() -> Path:
        raise AssertionError("рабочий стол не нужен, путь задан")

    monkeypatch.setattr(desktop, "desktop_dir", boom)
    assert journal_dir(str(tmp_path / "свой путь")) == tmp_path / "свой путь"


def test_тильда_в_настройке_раскрывается_в_домашнюю_папку(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = journal_dir("~/мой-журнал")
    assert "~" not in str(result)
    assert result == tmp_path / "мой-журнал"
