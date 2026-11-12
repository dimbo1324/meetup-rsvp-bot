@echo off
rem Быстрый старт rcbot: двойной щелчок. Политика выполнения PowerShell обходится только для этого скрипта.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0quickstart.ps1" %*
echo.
pause
