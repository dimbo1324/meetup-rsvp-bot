"""Точка входа CLI: разбор аргументов, коды выхода, вывод по-русски (SPEC 4)."""

from __future__ import annotations

import argparse
import io
import logging
import subprocess
import sys
from collections.abc import Callable

import pytest
from tests.unit.cli.conftest import write_env

from rcbot import __version__, cli
from rcbot.config.paths import Paths
from rcbot.config.secrets import SecretMissing

COMMANDS = {
    "init",
    "doctor",
    "discover",
    "login",
    "check",
    "run",
    "simulate",
    "purge",
    "uninstall",
}


def _only(handler: Callable[[argparse.Namespace], int]) -> Callable[[], dict[str, object]]:
    return lambda: dict.fromkeys(COMMANDS, handler)


def test_все_команды_спецификации_зарегистрированы() -> None:
    assert set(cli._handlers()) == COMMANDS


def test_без_команды_argparse_отказывает_кодом_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])
    assert exit_info.value.code == 2
    assert "команда" in capsys.readouterr().err


def test_неизвестная_команда_отклоняется_кодом_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["steal-slots"])
    assert exit_info.value.code == 2
    assert "steal-slots" in capsys.readouterr().err


def test_version_печатает_версию_и_выходит_кодом_0(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"rcbot {__version__}"


def test_неизвестная_опция_отклоняется() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["doctor", "--nonsense"])
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    ("argv", "attributes", "expected"),
    [
        (["doctor"], ("full", "offline"), (False, False)),
        (["doctor", "--full", "--offline"], ("full", "offline"), (True, True)),
        (["login"], ("check",), (False,)),
        (["login", "--check"], ("check",), (True,)),
        (["check", "--once"], ("once",), (True,)),
        (["simulate"], ("wait",), (600,)),
        (["simulate", "--wait", "45"], ("wait",), (45,)),
    ],
)
def test_аргументы_команд_доходят_до_обработчика(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    attributes: tuple[str, ...],
    expected: tuple[object, ...],
) -> None:
    seen: list[argparse.Namespace] = []

    def handler(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(cli, "_handlers", _only(handler))
    assert cli.main(argv) == 0
    assert seen[0].command == argv[0]
    assert tuple(getattr(seen[0], name) for name in attributes) == expected


@pytest.mark.parametrize("code", [0, 1, 3])
def test_код_возврата_обработчика_передаётся_как_есть(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(cli, "_handlers", _only(lambda _: code))
    assert cli.main(["purge"]) == code


def test_нет_секрета_даёт_код_2_и_подсказку(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(_: argparse.Namespace) -> int:
        raise SecretMissing("нет секрета «пароль gub.uy»: выполните `rcbot init`")

    monkeypatch.setattr(cli, "_handlers", _only(handler))
    assert cli.main(["login"]) == 2
    err = capsys.readouterr().err
    assert "Ошибка:" in err
    assert "rcbot init" in err


def test_ошибка_настроек_даёт_код_2_без_значений(
    paths: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    """Настоящий путь: .env с недопустимым значением → ValidationError из build_core."""
    secretish = "http://token-in-url-123456.example"
    write_env(paths, agenda_url=secretish, poll_interval_sec="5")
    assert cli.main(["doctor", "--offline"]) == 2
    err = capsys.readouterr().err
    assert "Ошибка настроек в .env" in err
    assert "agenda_url" in err
    assert "poll_interval_sec" in err
    assert "не может быть меньше 10" in err
    assert "token-in-url" not in err


def test_ctrl_c_даёт_код_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_handlers", _only(handler))
    assert cli.main(["run"]) == 130


def test_неожиданная_ошибка_не_теряется_а_попадает_в_лог_и_даёт_код_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Под pythonw.exe (Планировщик) консоли нет: без записи в лог причина падения не видна."""

    def handler(_: argparse.Namespace) -> int:
        raise RuntimeError("причина падения")

    monkeypatch.setattr(cli, "_handlers", _only(handler))
    with caplog.at_level(logging.ERROR):
        assert cli.main(["check"]) == 1
    assert "RuntimeError" in capsys.readouterr().err
    assert any(r.exc_info and r.exc_info[0] is RuntimeError for r in caplog.records)


def test_нет_секрета_под_pythonw_остаётся_в_логе(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(_: argparse.Namespace) -> int:
        raise SecretMissing("нет секрета «пароль gub.uy»")

    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(cli, "_handlers", _only(handler))
    with caplog.at_level(logging.ERROR):
        assert cli.main(["run"]) == 2
    assert any("нет секрета" in r.getMessage() for r in caplog.records)


def test_вывод_по_русски_не_падает_на_cp1252(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    monkeypatch.setattr(sys, "stdout", console)

    def handler(_: argparse.Namespace) -> int:
        print("Готово ✅ — всё хорошо")
        return 0

    monkeypatch.setattr(cli, "_handlers", _only(handler))
    assert cli.main(["purge"]) == 0
    # Консоль Windows переводит \n в \r\n: сравниваем текст, а не окончания строк
    text = raw.getvalue().decode("utf-8").replace("\r\n", "\n")
    assert text == "Готово ✅ — всё хорошо\n"


def test_без_потоков_как_в_pythonw_не_падает(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    def handler(_: argparse.Namespace) -> int:
        raise SecretMissing("нет секрета")

    monkeypatch.setattr(cli, "_handlers", _only(handler))
    assert cli.main(["run"]) == 2


def test_запуск_модулем_работает_как_консольная_команда() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "rcbot", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    assert done.returncode == 0
    assert done.stdout.strip() == f"rcbot {__version__}"
