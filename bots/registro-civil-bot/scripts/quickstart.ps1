# Быстрый старт rcbot под Windows 11: от чистого ноутбука до проверенного окружения.
# Что делает: проверяет uv (предлагает поставить через winget), ставит зависимости без инструментов
# разработки, при отсутствии Chrome и Edge предлагает встроенный Chromium, запускает мастер
# `rcbot init` (номер этапа, номер заявки, секреты в Credential Manager) и `rcbot doctor`,
# затем предлагает автозапуск при входе в Windows. Вход на сайт и разведку скрипт не запускает.
# Запуск: двойной щелчок по quickstart.cmd или
#   powershell -ExecutionPolicy Bypass -File bots\registro-civil-bot\scripts\quickstart.ps1
# -SkipInit пропускает мастер секретов, -SkipAutostart — вопрос про автозапуск.
param(
    [switch]$SkipInit,
    [switch]$SkipAutostart
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$botDir = Split-Path -Parent $PSScriptRoot
$repoRoot = (Resolve-Path (Join-Path $botDir '..\..')).Path

function Say([string]$text) { Write-Host $text }

function Ask-Yes([string]$question, [bool]$default) {
    $hint = if ($default) { '[Д/н]' } else { '[д/Н]' }
    $answer = (Read-Host "$question $hint").Trim().ToLower()
    if (-not $answer) { return $default }
    return ($answer -in @('д', 'да', 'y', 'yes'))
}

# Те же каталоги и пути, что _WINDOWS_ROOTS и _WINDOWS_PARTS в src/rcbot/infrastructure/gubuy/
# browser.py: Python в окружении ещё может не быть. Меняете там — поменяйте и здесь.
function Find-Browser {
    $roots = @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    $known = @(
        @{ Name = 'Google Chrome'; Path = 'Google\Chrome\Application\chrome.exe' },
        @{ Name = 'Microsoft Edge'; Path = 'Microsoft\Edge\Application\msedge.exe' }
    )
    foreach ($browser in $known) {
        foreach ($root in $roots) {
            if (Test-Path (Join-Path $root $browser.Path)) { return $browser.Name }
        }
    }
    return $null
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

Say ''
Say 'rcbot: быстрый старт под Windows 11'
Say '-----------------------------------'

if ([Environment]::OSVersion.Version.Build -lt 22000) {
    Say 'Внимание: бот рассчитан на Windows 11; на этой версии не проверялся.'
}

Say ''
Say '[1/5] Менеджер пакетов uv'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say 'uv не найден: он ставит Python 3.14 и зависимости бота.'
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Нет ни uv, ни winget. Установите uv по инструкции https://docs.astral.sh/uv/ и запустите скрипт снова.'
    }
    if (-not (Ask-Yes 'Поставить uv через winget (пакет astral-sh.uv)?' $true)) {
        throw 'Без uv продолжать нельзя. Поставьте его и запустите скрипт снова.'
    }
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw 'winget не смог поставить uv' }
    Update-SessionPath
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv поставлен, но в этом окне ещё не виден. Закройте окно и запустите скрипт заново.'
    }
}
Say 'uv есть.'

Push-Location $repoRoot
try {
    Say ''
    Say '[2/5] Зависимости (Python 3.14 uv скачает сам; это занимает несколько минут)'
    uv sync --frozen --all-packages --no-dev
    if ($LASTEXITCODE -ne 0) { throw 'uv sync завершился с ошибкой' }

    Say ''
    Say '[3/5] Браузер'
    $found = Find-Browser
    if ($found) {
        Say "Найден ${found}: встроенный Chromium не нужен (RCBOT_BROWSER=auto берёт Chrome, затем Edge)."
    } else {
        Say 'Ни Google Chrome, ни Microsoft Edge не найдены.'
        Say 'Лучше установить Google Chrome. Либо бот скачает свой Chromium (около 740 МБ).'
        if (Ask-Yes 'Скачать встроенный Chromium сейчас?' $true) {
            $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $botDir '.browsers'
            uv run --no-sync playwright install chromium
            if ($LASTEXITCODE -ne 0) { throw 'не удалось скачать Chromium' }
        } else {
            Say 'Пропущено: установите Chrome или Edge и запустите `rcbot doctor`.'
        }
    }

    Say ''
    Say '[4/5] Данные бота (мастер: номер этапа, номер заявки, секреты)'
    if ($SkipInit) {
        Say 'Пропущено (-SkipInit). Когда будете готовы: rcbot.cmd init'
    } else {
        Say 'Секреты вводятся скрыто и кладутся в Диспетчер учётных данных Windows, не в файлы.'
        uv run --no-sync rcbot init
        if ($LASTEXITCODE -ne 0) {
            Say 'Мастер не закончил: повторите `rcbot.cmd init` (введённое раньше сохранилось).'
        }
    }

    Say ''
    Say '[5/5] Проверка окружения'
    uv run --no-sync rcbot doctor --offline
    $doctorOk = ($LASTEXITCODE -eq 0)
    if (-not $doctorOk) {
        Say 'doctor нашёл, что поправить (строки НЕТ выше). Исправьте и запустите `rcbot.cmd doctor` снова.'
    }
} finally {
    Pop-Location
}

if (-not $SkipAutostart) {
    Say ''
    Say 'Автозапуск: бот будет стартовать при входе в Windows и перезапускаться при сбое.'
    if (Ask-Yes 'Включить автозапуск сейчас?' $false) {
        & (Join-Path $PSScriptRoot 'register_task.ps1')
    } else {
        Say 'Позже: powershell -ExecutionPolicy Bypass -File bots\registro-civil-bot\scripts\register_task.ps1'
    }
}

Say ''
Say 'Дальше (команды — через scripts\rcbot.cmd из любой папки):'
Say '  rcbot.cmd doctor        полная проверка, включая доступ к сайтам (без --offline)'
Say '  rcbot.cmd login --check вход на сайт: пароль берётся из Диспетчера учётных данных'
Say '  rcbot.cmd check --once  одна проверка календаря'
Say '  rcbot.cmd simulate      тестовая тревога во все каналы'
Say '  rcbot.cmd run           работа в этом окне (для постоянной работы — автозапуск)'
Say 'Подробно: bots\registro-civil-bot\README.md'
