# Установка rcbot на ноутбуке (SPEC 4.2–4.3) из монорепозитория: зависимости, Chromium, .env,
# затем мастер секретов и проверка окружения. Автозапуск — отдельно: register_task.ps1.
# Запуск: powershell -ExecutionPolicy Bypass -File bots\registro-civil-bot\scripts\install.ps1
$ErrorActionPreference = 'Stop'

$botDir = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $botDir '..\..')).Path
Push-Location $repoRoot
try {
    uv run python dev.py setup --bot registro-civil-bot
    if ($LASTEXITCODE -ne 0) { throw 'dev.py setup завершился с ошибкой' }
    # Окружение уже собрано по uv.lock шагом выше: здесь ничего не переразрешается и не ставится
    uv run --no-sync rcbot init
    uv run --no-sync rcbot doctor
} finally {
    Pop-Location
}
