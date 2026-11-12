@echo off
rem Короткий вызов rcbot из любой папки: rcbot.cmd doctor, rcbot.cmd run, rcbot.cmd check --once
rem Окружение не пересобирается (--no-sync): его готовит quickstart.cmd.
chcp 65001 >nul
pushd "%~dp0..\..\.."
uv run --no-sync rcbot %*
set "RCBOT_EXIT=%ERRORLEVEL%"
popd
exit /b %RCBOT_EXIT%
