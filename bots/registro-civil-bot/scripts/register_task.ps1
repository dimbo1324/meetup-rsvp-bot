# Автозапуск rcbot через Планировщик заданий Windows (SPEC 4.6, решение R3).
# Задание: при входе пользователя, pythonw.exe -m rcbot run (без окна консоли), перезапуск
# при сбое каждую минуту до 999 раз, без ограничения по времени, работа и от батареи.
# Запуск: powershell -ExecutionPolicy Bypass -File bots\registro-civil-bot\scripts\register_task.ps1
$ErrorActionPreference = 'Stop'
$TaskName = 'rcbot'

$botDir = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $botDir '..\..')).Path
$pythonw = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    throw "Не найден $pythonw. Сначала из корня репозитория: uv run python dev.py setup"
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m rcbot run' -WorkingDirectory $botDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'rcbot: наблюдение за записью в Registro Civil' -Force | Out-Null

Write-Host "Задание $TaskName зарегистрировано. Запустить сейчас: Start-ScheduledTask -TaskName $TaskName"
Write-Host 'Питание: при работе от сети «Переводить в спящий режим» — Никогда; закрытие крышки — «Ничего не делать».'
