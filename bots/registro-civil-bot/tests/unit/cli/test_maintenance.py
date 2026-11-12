"""`rcbot purge` и `rcbot uninstall` (SPEC 2.7, 4.7, критерий приёмки 10)."""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rcbot import cli
from rcbot.cli import maintenance
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretKey, SecretStore
from rcbot.infrastructure.win.single_instance import SingleInstance

# --- purge ---

CLEARED = ("logs", "data/tmp", "data/quarantine", "data/shots", "data/discovery", ".cache")
KEPT = ("data/rcbot.sqlite3", ".env", ".browsers/chromium/chrome.exe", "alembic.ini")


def populate(home: Path) -> None:
    for name in (*CLEARED, "data/shots/deep"):
        (home / name).mkdir(parents=True, exist_ok=True)
    for name in CLEARED:
        (home / name / "file.dat").write_text("x", encoding="utf-8")
    (home / "data/shots/deep/nested.png").write_bytes(b"png")
    for name in KEPT:
        target = home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("важное", encoding="utf-8")


def test_purge_удаляет_временное_и_не_трогает_базу_env_и_браузер(
    bot_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(bot_home)

    assert cli.main(["purge"]) == 0

    out = capsys.readouterr().out
    for name in CLEARED:
        assert not (bot_home / name).exists(), name
        assert f"Удалено: {bot_home / name}" in out
    for name in KEPT:
        assert (bot_home / name).read_text(encoding="utf-8") == "важное", name
    assert "База данных, .env и Chromium не тронуты." in out


def test_purge_частично_заполненного_каталога_называет_только_удалённое(
    bot_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (bot_home / "logs").mkdir()
    cli.main(["purge"])
    out = capsys.readouterr().out
    assert out.count("Удалено:") == 1
    assert "Удалять нечего" not in out


def test_purge_на_чистом_каталоге_говорит_что_удалять_нечего(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["purge"]) == 0
    out = capsys.readouterr().out
    assert "Удалять нечего." in out
    assert "Удалено:" not in out


def test_purge_повторный_запуск_безопасен(bot_home: Path) -> None:
    populate(bot_home)
    assert cli.main(["purge"]) == 0
    assert cli.main(["purge"]) == 0
    assert (bot_home / "data/rcbot.sqlite3").exists()


def test_purge_отказывает_пока_бот_запущен_и_ничего_не_удаляет(
    bot_home: Path, paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(bot_home)
    running = SingleInstance(paths.lock_file)
    running.acquire()
    try:
        assert cli.main(["purge"]) == 3
    finally:
        running.release()

    assert "Бот запущен" in capsys.readouterr().out
    for name in CLEARED:
        assert (bot_home / name / "file.dat").exists(), name


def test_purge_не_оставляет_замок_занятым(paths: Paths) -> None:
    cli.main(["purge"])
    probe = SingleInstance(paths.lock_file)
    probe.acquire()
    probe.release()


# --- uninstall ---


class FakeSchtasks:
    """Подмена subprocess.run: Планировщик Windows не затрагивается (SPEC 4.6)."""

    def __init__(self, *, exists: bool = True, delete_code: int = 0) -> None:
        self.exists = exists
        self.delete_code = delete_code
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        self.commands.append(command)
        assert command[0] == "schtasks", f"неожиданный внешний процесс: {command}"
        if "/Query" in command:
            return SimpleNamespace(returncode=0 if self.exists else 1, stdout="", stderr="")
        assert "/Delete" in command
        return SimpleNamespace(returncode=self.delete_code, stdout="", stderr="отказано")


@pytest.fixture
def schtasks(monkeypatch: pytest.MonkeyPatch) -> FakeSchtasks:
    fake = FakeSchtasks()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def fill_all_secrets() -> None:
    store = SecretStore()
    for key in SecretKey:
        store.set(key, f"synthetic-{key.value}-value")


def present() -> list[SecretKey]:
    return [key for key, ok in SecretStore().present().items() if ok]


def answer(monkeypatch: pytest.MonkeyPatch, text: str) -> list[str]:
    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        return text

    monkeypatch.setattr(builtins, "input", fake_input)
    return asked


@pytest.mark.skipif(sys.platform != "win32", reason="задание Планировщика есть только в Windows")
@pytest.mark.parametrize("yes", ["да", "y", "yes", "д", "Да", "  YES  ", "Y\n"])
def test_uninstall_после_подтверждения_удаляет_все_секреты_и_задание(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    schtasks: FakeSchtasks,
    yes: str,
) -> None:
    fill_all_secrets()
    asked = answer(monkeypatch, yes)

    assert cli.main(["uninstall"]) == 0

    out = capsys.readouterr().out
    assert present() == []  # критерий 10: секретов в Credential Manager не осталось
    for key in SecretKey:
        assert f"Секрет удалён: {key.value}" in out
    assert "synthetic-" not in out
    assert "Задание Планировщика удалено." in out
    assert schtasks.commands[-1] == ["schtasks", "/Delete", "/TN", "rcbot", "/F"]
    assert "[да/нет]" in asked[0]
    assert "rcbot purge" in out


@pytest.mark.parametrize("no", ["", "нет", "n", "no", "ok", "yess", "дa ", "да да", "0"])
def test_uninstall_всё_кроме_да_отменяет_без_последствий(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    schtasks: FakeSchtasks,
    no: str,
) -> None:
    fill_all_secrets()
    answer(monkeypatch, no)

    code = cli.main(["uninstall"])

    # «дa » здесь с латинской «a» — не русская «да»; пробелы по краям срезаются
    assert code == 1
    assert "Отменено." in capsys.readouterr().out
    assert len(present()) == len(SecretKey)
    assert schtasks.commands == []


@pytest.mark.skipif(sys.platform != "win32", reason="задание Планировщика есть только в Windows")
def test_uninstall_нет_задания_планировщика_это_не_ошибка(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeSchtasks(exists=False)
    monkeypatch.setattr(subprocess, "run", fake)
    fill_all_secrets()
    answer(monkeypatch, "да")

    assert cli.main(["uninstall"]) == 0

    assert "Задания Планировщика нет." in capsys.readouterr().out
    assert not any("/Delete" in c for c in fake.commands)
    assert present() == []


@pytest.mark.skipif(sys.platform != "win32", reason="задание Планировщика есть только в Windows")
def test_uninstall_если_задание_не_удалилось_это_видно_и_код_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Критерий 10: «задания нет» нельзя говорить, когда его просто не удалось удалить."""
    monkeypatch.setattr(subprocess, "run", FakeSchtasks(exists=True, delete_code=1))
    fill_all_secrets()
    answer(monkeypatch, "да")

    code = cli.main(["uninstall"])

    captured = capsys.readouterr()
    assert code == 1
    assert "Задания Планировщика нет" not in captured.out
    assert "schtasks /Delete /TN rcbot /F" in captured.err
    assert present() == []  # секреты всё равно удалены


def test_uninstall_удаляет_только_существующие_секреты(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], schtasks: FakeSchtasks
) -> None:
    SecretStore().set(SecretKey.PASSWORD, "synthetic-pass")
    answer(monkeypatch, "да")
    assert cli.main(["uninstall"]) == 0
    out = capsys.readouterr().out
    assert out.count("Секрет удалён:") == 1
    assert f"Секрет удалён: {SecretKey.PASSWORD.value}" in out


def test_uninstall_на_не_windows_планировщик_не_трогает(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], schtasks: FakeSchtasks
) -> None:
    monkeypatch.setattr(maintenance, "_not_running", lambda paths: True)
    monkeypatch.setattr(sys, "platform", "linux")
    fill_all_secrets()
    answer(monkeypatch, "да")

    assert cli.main(["uninstall"]) == 0

    assert schtasks.commands == []
    assert "Планировщика" not in capsys.readouterr().out
    assert present() == []


def test_uninstall_отказывает_пока_бот_запущен(
    monkeypatch: pytest.MonkeyPatch,
    paths: Paths,
    capsys: pytest.CaptureFixture[str],
    schtasks: FakeSchtasks,
) -> None:
    fill_all_secrets()
    asked = answer(monkeypatch, "да")
    running = SingleInstance(paths.lock_file)
    running.acquire()
    try:
        assert cli.main(["uninstall"]) == 3
    finally:
        running.release()
    assert asked == []  # даже не спрашивает
    assert len(present()) == len(SecretKey)
    assert schtasks.commands == []
    assert "Бот запущен" in capsys.readouterr().out


def test_uninstall_без_консоли_не_падает_а_пишет_в_лог_и_ничего_не_удаляет(
    monkeypatch: pytest.MonkeyPatch, schtasks: FakeSchtasks, caplog: pytest.LogCaptureFixture
) -> None:
    """Под pythonw.exe stdin нет: input() падает, секреты остаются на месте."""

    def no_stdin(prompt: str = "") -> str:
        raise RuntimeError("lost sys.stdin")

    monkeypatch.setattr(builtins, "input", no_stdin)
    fill_all_secrets()
    assert cli.main(["uninstall"]) == 1
    assert len(present()) == len(SecretKey)
    assert schtasks.commands == []
