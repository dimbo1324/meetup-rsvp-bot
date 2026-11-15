"""Быстрый старт под Windows 11: quickstart.ps1, quickstart.cmd, rcbot.cmd.

Скрипт исполняет настоящий Windows PowerShell 5.1 с теми же подменами, что и тесты остальных
скриптов (`test_scripts.py`): `uv`, `winget`, `Read-Host` и командлеты Планировщика записывают
вызовы и ничего не делают, поэтому ни зависимости, ни задания на машине не меняются. Браузер
быстрому старту не нужен (решение R13): бот читает страницу SAE обычным HTTPS-запросом.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tests.conftest import BOT_DIR
from tests.integration.process.test_scripts import (  # noqa: F401 — модуль-фикстура pytestmark
    POWERSHELL,
    SCRIPTS,
    calls,
    execute,
    pytestmark,
    said,
    same_path,
)

QUICKSTART = SCRIPTS / "quickstart.ps1"
SYNC = "sync --frozen --all-packages --no-dev"
INIT = "run --no-sync rcbot init"
DOCTOR = "run --no-sync rcbot doctor --offline"
YES, NO = "д", "н"


def raw_calls(result: dict[str, Any], name: str) -> list[dict[str, Any]]:
    found = result["calls"]
    found = [found] if isinstance(found, dict) else found
    return [c for c in found if c["name"] == name]


def uv_args(result: dict[str, Any]) -> list[str]:
    return [c["params"]["args"] for c in raw_calls(result, "uv")]


def quickstart(tmp_path: Path, **env: str) -> dict[str, Any]:
    env.setdefault("RC_UV_CODES", "0")
    return execute(QUICKSTART, tmp_path, **env)


def test_ставит_зависимости_без_dev_и_не_ищет_браузер(tmp_path: Path) -> None:
    result = quickstart(tmp_path)

    assert result["error"] is None
    assert uv_args(result) == [SYNC, INIT, DOCTOR]
    assert all(same_path(c["cwd"], BOT_DIR.parents[1]) for c in raw_calls(result, "uv"))
    assert result["location_restored"] is True
    assert "Chrome" not in said(result) and "Chromium" not in said(result)
    assert calls(result, "Register-ScheduledTask") == []  # автозапуск без согласия не включается


def test_при_сбое_установки_зависимостей_останавливается(tmp_path: Path) -> None:
    result = quickstart(tmp_path, RC_UV_CODES="1")

    assert "uv sync завершился с ошибкой" in result["error"]
    assert uv_args(result) == [SYNC]
    assert result["location_restored"] is True


def test_незаконченный_мастер_не_обрывает_проверку(tmp_path: Path) -> None:
    result = quickstart(tmp_path, RC_UV_CODES="0,1,0")

    assert result["error"] is None
    assert uv_args(result) == [SYNC, INIT, DOCTOR]
    assert "Мастер не закончил" in said(result)


def test_замечания_doctor_показываются_но_скрипт_завершается(tmp_path: Path) -> None:
    result = quickstart(tmp_path, RC_UV_CODES="0,0,1")

    assert result["error"] is None
    assert "doctor нашёл, что поправить" in said(result)
    assert "Дальше" in said(result)  # инструкция печатается и после замечаний


def test_skipinit_пропускает_мастер(tmp_path: Path) -> None:
    result = quickstart(tmp_path, RC_SWITCHES="SkipInit")

    assert uv_args(result) == [SYNC, DOCTOR]
    assert "rcbot.cmd init" in said(result)


def test_автозапуск_включается_только_по_согласию(tmp_path: Path) -> None:
    result = quickstart(tmp_path, RC_ANSWERS=YES)

    assert result["error"] is None
    [action] = calls(result, "New-ScheduledTaskAction")
    assert action["Argument"] == "-m rcbot run"
    assert len(calls(result, "Register-ScheduledTask")) == 1


def test_skipautostart_не_спрашивает_про_автозапуск(tmp_path: Path) -> None:
    result = quickstart(tmp_path, RC_SWITCHES="SkipAutostart")

    assert not any("автозапуск" in p["prompt"] for p in calls(result, "Read-Host"))
    assert calls(result, "Register-ScheduledTask") == []


def test_без_uv_ставит_его_через_winget_по_согласию(tmp_path: Path) -> None:
    result = quickstart(
        tmp_path,
        RC_NO_UV="1",
        RC_WINGET_INSTALLS_UV="1",
        RC_ANSWERS=YES,
    )

    assert result["error"] is None
    [winget] = calls(result, "winget")
    assert winget["args"].startswith("install --id astral-sh.uv -e")
    assert uv_args(result)[0] == SYNC


def test_без_uv_и_без_согласия_останавливается_ничего_не_ставя(tmp_path: Path) -> None:
    result = quickstart(tmp_path, RC_NO_UV="1", RC_ANSWERS=NO)

    assert "Без uv продолжать нельзя" in result["error"]
    assert calls(result, "winget") == []
    assert raw_calls(result, "uv") == []


def test_если_после_winget_uv_не_виден_просит_новое_окно(tmp_path: Path) -> None:
    result = quickstart(tmp_path, RC_NO_UV="1", RC_ANSWERS=YES)

    assert "Закройте окно и запустите скрипт заново" in result["error"]
    assert len(calls(result, "winget")) == 1


def test_печатает_следующие_шаги_с_короткими_командами(tmp_path: Path) -> None:
    text = said(quickstart(tmp_path))
    for command in ("rcbot.cmd doctor", "check --once", "simulate", "run"):
        assert command in text
    assert "login" not in text


def test_секреты_скрипт_не_спрашивает_их_спрашивает_только_rcbot_init(tmp_path: Path) -> None:
    result = quickstart(tmp_path)
    prompts = " ".join(p["prompt"] for p in calls(result, "Read-Host")).lower()
    assert "парол" not in prompts and "токен" not in prompts


def test_quickstart_cmd_обходит_политику_выполнения_только_для_скрипта() -> None:
    text = (SCRIPTS / "quickstart.cmd").read_text(encoding="utf-8")
    assert "-ExecutionPolicy Bypass" in text and "quickstart.ps1" in text
    assert "Set-ExecutionPolicy" not in text  # системная политика не меняется


def test_rcbot_cmd_запускает_rcbot_из_корня_и_передаёт_код_возврата() -> None:
    text = (SCRIPTS / "rcbot.cmd").read_text(encoding="utf-8")
    assert "uv run --no-sync rcbot %*" in text and "exit /b %RCBOT_EXIT%" in text


def test_rcbot_cmd_настоящий_запуск_help_из_любой_папки(tmp_path: Path) -> None:
    done = subprocess.run(
        ["cmd", "/c", str(SCRIPTS / "rcbot.cmd"), "--help"],
        capture_output=True,
        check=False,
        timeout=120,
        cwd=tmp_path,
    )
    out = done.stdout.decode("utf-8", errors="replace")
    assert done.returncode == 0, out + done.stderr.decode("utf-8", errors="replace")
    assert "doctor" in out and "simulate" in out
