"""Скрипты scripts/*.ps1 (SPEC 4.2, 4.6, 4.7, решение R3).

Скрипты разбирает настоящий Windows PowerShell 5.1. Содержание проверяется исполнением: командлеты
Планировщика (`New-ScheduledTask*`, `Register-ScheduledTask`, ...) подменены записывающими
функциями, которые имеют приоритет над настоящими, поэтому в реальном Планировщике ничего не
создаётся и не удаляется. Перед каждым исполнением harness проверяет, что подмена
действительно перекрывает командлет (иначе тест падает, не запустив скрипт).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import BOT_DIR

SCRIPTS = BOT_DIR / "scripts"
POWERSHELL = shutil.which("powershell")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None, reason="нужен Windows PowerShell"
)

BOM = b"\xef\xbb\xbf"
ALL_SCRIPTS = sorted(p.name for p in SCRIPTS.glob("*.ps1"))

# Подмены записывают вызов (имя и именованные параметры) в $global:rec и ничего не делают
STUBS = r"""
$global:rec = [ordered]@{ calls = @(); error = $null; location = $null }
function global:Rec($name, $bound) {
    $o = [ordered]@{}
    foreach ($k in $bound.Keys) {
        $v = $bound[$k]
        if ($v -is [TimeSpan]) { $v = $v.TotalSeconds }
        elseif ($v -is [System.Management.Automation.SwitchParameter]) { $v = [bool]$v }
        elseif ($v -isnot [string] -and $v -isnot [int] -and $v -isnot [bool]) { $v = [string]$v }
        $o[$k] = $v
    }
    $global:rec.calls += [pscustomobject]@{ name = $name; params = $o; cwd = (Get-Location).Path }
}
function global:New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory)
    Rec 'New-ScheduledTaskAction' $PSBoundParameters; 'ACTION' }
function global:New-ScheduledTaskTrigger { param([switch]$AtLogOn, $User)
    Rec 'New-ScheduledTaskTrigger' $PSBoundParameters; 'TRIGGER' }
function global:New-ScheduledTaskSettingsSet {
    param($RestartCount, $RestartInterval, $ExecutionTimeLimit, [switch]$AllowStartIfOnBatteries,
          [switch]$DontStopIfGoingOnBatteries, [switch]$StartWhenAvailable, $MultipleInstances)
    Rec 'New-ScheduledTaskSettingsSet' $PSBoundParameters; 'SETTINGS' }
function global:New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel)
    Rec 'New-ScheduledTaskPrincipal' $PSBoundParameters; 'PRINCIPAL' }
function global:Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description, [switch]$Force)
    Rec 'Register-ScheduledTask' $PSBoundParameters; 'REGISTERED' }
function global:Get-ScheduledTask { param($TaskName, $ErrorAction)
    Rec 'Get-ScheduledTask' $PSBoundParameters
    if ($env:RC_TASK_EXISTS -eq '1') { 'TASK' } }
function global:Stop-ScheduledTask { param($TaskName, $ErrorAction)
    Rec 'Stop-ScheduledTask' $PSBoundParameters }
function global:Unregister-ScheduledTask { param($TaskName, $Confirm)
    Rec 'Unregister-ScheduledTask' $PSBoundParameters }
function global:Write-Host { param([Parameter(ValueFromRemainingArguments)]$Object)
    Rec 'Write-Host' @{ text = ($Object -join ' ') } }
function global:uv {
    Rec 'uv' @{ args = ($args -join ' ') }
    $codes = @($env:RC_UV_CODES -split ',')
    $index = @($global:rec.calls | Where-Object { $_.name -eq 'uv' }).Count - 1
    $global:LASTEXITCODE = [int]$codes[[Math]::Min($index, $codes.Count - 1)]
}
$global:answers = @($env:RC_ANSWERS -split '\|' | Where-Object { $_ -ne '' })
function global:Read-Host { param($Prompt)
    Rec 'Read-Host' @{ prompt = [string]$Prompt }
    if ($global:answers.Count -eq 0) { return '' }
    $first = $global:answers[0]
    $global:answers = @($global:answers | Select-Object -Skip 1)
    return $first }
function global:winget {
    Rec 'winget' @{ args = ($args -join ' ') }
    $global:LASTEXITCODE = 0
    if ($env:RC_WINGET_INSTALLS_UV -eq '1') {
        Set-Item -Path function:global:uv -Value $global:uvBlock } }
function global:Test-Path { param([Parameter(Position = 0)]$Path, $PathType, $LiteralPath)
    # Chrome и Edge на машине разработчика настоящие: «что установлено» задаёт тест (RC_BROWSERS)
    if ($env:RC_BROWSERS -and "$Path" -match '(chrome|msedge)\.exe$') {
        return (@($env:RC_BROWSERS -split '\|') -contains (Split-Path "$Path" -Leaf)) }
    Microsoft.PowerShell.Management\Test-Path @PSBoundParameters }
foreach ($cmd in 'Register-ScheduledTask', 'Unregister-ScheduledTask', 'New-ScheduledTaskAction',
                 'Stop-ScheduledTask', 'Get-ScheduledTask', 'uv') {
    if ((Get-Command $cmd).CommandType -ne 'Function') {
        throw "подмена $cmd не перекрывает настоящий командлет — исполнение прервано"
    }
}
"""

RUN = r"""
$before = (Get-Location).Path
$global:uvBlock = ${function:uv}
if ($env:RC_NO_UV -eq '1') {
    Remove-Item function:uv
    # На машине разработчика настоящий uv.exe есть: «uv не установлен» нужно изобразить
    function global:Get-Command { param($Name, $ErrorAction)
        if ($Name -eq 'uv' -and -not (Test-Path function:uv)) { return $null }
        Microsoft.PowerShell.Core\Get-Command @PSBoundParameters } }
$switches = @{}
foreach ($name in ($env:RC_SWITCHES -split ',')) { if ($name) { $switches[$name] = $true } }
try { & $env:RC_SCRIPT @switches } catch { $global:rec.error = $_.Exception.Message }
$global:rec.location_restored = ((Get-Location).Path -eq $before)
$global:rec | ConvertTo-Json -Depth 8 | Set-Content -Path $env:RC_OUT -Encoding UTF8
"""

PARSE = r"""
$errors = $null; $tokens = $null
$parser = [System.Management.Automation.Language.Parser]
[void]$parser::ParseFile($env:RC_SCRIPT, [ref]$tokens, [ref]$errors)
$list = @($errors | ForEach-Object { "$($_.Extent.StartLineNumber): $($_.Message)" })
ConvertTo-Json -InputObject $list | Set-Content -Path $env:RC_OUT -Encoding UTF8
"""


def run_powershell(
    body: str, script: Path, tmp_path: Path, **env: str
) -> dict[str, Any] | list[str]:
    harness = tmp_path / "harness.ps1"
    harness.write_bytes(BOM + body.encode("utf-8"))
    out = tmp_path / "out.json"
    full_env = {**os.environ, "RC_SCRIPT": str(script), "RC_OUT": str(out), **env}
    done = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
        env=full_env,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    data = json.loads(out.read_text(encoding="utf-8-sig") or "null")
    return [] if data is None else data


def execute(script: Path, tmp_path: Path, **env: str) -> dict[str, Any]:
    result = run_powershell(STUBS + RUN, script, tmp_path, **env)
    assert isinstance(result, dict)
    return result


def calls(result: dict[str, Any], name: str) -> list[dict[str, Any]]:
    found = result["calls"]
    found = [found] if isinstance(found, dict) else found
    return [c["params"] for c in found if c["name"] == name]


def said(result: dict[str, Any]) -> str:
    return "\n".join(p["text"] for p in calls(result, "Write-Host"))


def same_path(a: str, b: Path) -> bool:
    return Path(a).resolve() == b.resolve()


# --- форма файлов ---


def test_набор_скриптов_известен() -> None:
    assert ALL_SCRIPTS == [
        "quickstart.ps1",
        "register_task.ps1",
        "unregister_task.ps1",
    ]


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_скрипт_с_bom_иначе_powershell_5_1_читает_кириллицу_как_ansi(name: str) -> None:
    data = (SCRIPTS / name).read_bytes()
    assert data.startswith(BOM)
    data[len(BOM) :].decode("utf-8")  # и после BOM — корректный UTF-8


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_синтаксис_разбирается_настоящим_powershell_без_ошибок(name: str, tmp_path: Path) -> None:
    errors = run_powershell(PARSE, SCRIPTS / name, tmp_path)
    assert errors == []


def test_разбор_действительно_находит_ошибки(tmp_path: Path) -> None:
    """Проверка самого harness: сломанный скрипт не должен проходить как годный."""
    broken = tmp_path / "broken.ps1"
    broken.write_bytes(BOM + "if ($true) { Write-Host 'нет закрывающей скобки'\n".encode())
    errors = run_powershell(PARSE, broken, tmp_path)
    assert errors


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_скрипты_останавливаются_на_первой_ошибке(name: str) -> None:
    assert "$ErrorActionPreference = 'Stop'" in (SCRIPTS / name).read_text(encoding="utf-8-sig")


# --- register_task.ps1 ---


def test_register_task_создаёт_задание_по_spec_4_6(tmp_path: Path) -> None:
    result = execute(SCRIPTS / "register_task.ps1", tmp_path)

    assert result["error"] is None
    repo_root = BOT_DIR.parents[1]
    [action] = calls(result, "New-ScheduledTaskAction")
    assert same_path(action["Execute"], repo_root / ".venv" / "Scripts" / "pythonw.exe")
    assert action["Execute"].lower().endswith("pythonw.exe")  # без окна консоли
    assert action["Argument"] == "-m rcbot run"
    assert same_path(action["WorkingDirectory"], BOT_DIR)

    [trigger] = calls(result, "New-ScheduledTaskTrigger")
    assert trigger["AtLogOn"] is True
    assert trigger["User"] == os.environ["USERNAME"]

    [settings] = calls(result, "New-ScheduledTaskSettingsSet")
    assert settings["RestartCount"] == 999
    assert settings["RestartInterval"] == 60  # одна минута
    assert settings["ExecutionTimeLimit"] == 0  # без ограничения по времени
    assert settings["AllowStartIfOnBatteries"] is True
    assert settings["DontStopIfGoingOnBatteries"] is True
    assert settings["StartWhenAvailable"] is True
    assert settings["MultipleInstances"] == "IgnoreNew"

    [principal] = calls(result, "New-ScheduledTaskPrincipal")
    assert principal["UserId"] == os.environ["USERNAME"]
    assert principal["LogonType"] == "Interactive"
    assert principal["RunLevel"] == "Limited"  # права администратора не нужны

    [register] = calls(result, "Register-ScheduledTask")
    assert register["TaskName"] == "rcbot"
    assert register["Force"] is True
    assert register["Action"] == "ACTION"
    assert register["Trigger"] == "TRIGGER"
    assert register["Settings"] == "SETTINGS"
    assert register["Principal"] == "PRINCIPAL"
    assert "Start-ScheduledTask -TaskName rcbot" in said(result)


def test_register_task_без_venv_падает_до_регистрации(tmp_path: Path) -> None:
    """Корень репозитория без .venv: понятная ошибка, а не задание, которое не запустится."""
    scripts = tmp_path / "repo" / "bots" / "registro-civil-bot" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPTS / "register_task.ps1", scripts)

    result = execute(scripts / "register_task.ps1", tmp_path)

    assert "Не найден" in result["error"]
    assert "pythonw.exe" in result["error"]
    assert calls(result, "Register-ScheduledTask") == []
    assert calls(result, "New-ScheduledTaskAction") == []


# --- unregister_task.ps1 ---


def test_unregister_task_останавливает_и_удаляет_существующее_задание(tmp_path: Path) -> None:
    result = execute(SCRIPTS / "unregister_task.ps1", tmp_path, RC_TASK_EXISTS="1")

    assert result["error"] is None
    assert [c["TaskName"] for c in calls(result, "Get-ScheduledTask")] == ["rcbot"]
    assert [c["TaskName"] for c in calls(result, "Stop-ScheduledTask")] == ["rcbot"]
    [unregister] = calls(result, "Unregister-ScheduledTask")
    assert unregister["TaskName"] == "rcbot"
    assert unregister["Confirm"] is False  # без интерактивного вопроса
    assert "Задание rcbot удалено." in said(result)


def test_unregister_task_без_задания_ничего_не_удаляет_и_не_падает(tmp_path: Path) -> None:
    result = execute(SCRIPTS / "unregister_task.ps1", tmp_path, RC_TASK_EXISTS="0")

    assert result["error"] is None
    assert calls(result, "Stop-ScheduledTask") == []
    assert calls(result, "Unregister-ScheduledTask") == []
    assert "Задания rcbot нет." in said(result)
