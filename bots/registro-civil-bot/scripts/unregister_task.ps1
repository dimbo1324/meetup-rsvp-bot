# Удаление задания Планировщика rcbot (SPEC 4.7). Секреты удаляет `rcbot uninstall`.
$ErrorActionPreference = 'Stop'

if (Get-ScheduledTask -TaskName 'rcbot' -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName 'rcbot' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'rcbot' -Confirm:$false
    Write-Host 'Задание rcbot удалено.'
} else {
    Write-Host 'Задания rcbot нет.'
}
