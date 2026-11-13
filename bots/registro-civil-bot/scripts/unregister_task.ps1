# Удаление задания Планировщика rcbot (SPEC 4.7). Секреты удаляет `rcbot uninstall`.
$ErrorActionPreference = 'Stop'
$TaskName = 'rcbot'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Задание $TaskName удалено."
} else {
    Write-Host "Задания $TaskName нет."
}
