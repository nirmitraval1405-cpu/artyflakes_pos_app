param(
    [string]$Time = "22:30",
    [switch]$Unregister,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$TaskName = "Artyflakes DB Backup"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed scheduled task '$TaskName' (if it existed)."
    exit 0
}

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Started '$TaskName'. Check Task Scheduler for its result."
    exit 0
}

if ($Time -notmatch '^(?:[01]\d|2[0-3]):[0-5]\d$') {
    throw "Enter the daily run time as 24-hour HH:mm, for example 22:30."
}

$BackupScript = Join-Path $PSScriptRoot "backup.ps1"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Action = New-ScheduledTaskAction -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$BackupScript`""
$Trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($Time, "HH:mm", $null))
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal -Description "Verified daily Artyflakes PostgreSQL backup"

Write-Output "Registered '$TaskName' to run daily at $Time."
Write-Output "Check status and Last Run Result in Task Scheduler > Task Scheduler Library."
