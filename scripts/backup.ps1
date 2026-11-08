$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot "venv\Scripts\python.exe"
$ManagePy = Join-Path $ProjectRoot "manage.py"
$TaskLog = Join-Path $PSScriptRoot "backup-task.log"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Error "Project virtual-environment Python was not found at $Python"
    exit 1
}

Set-Location -LiteralPath $ProjectRoot
& $Python $ManagePy backup_db *>> $TaskLog
exit $LASTEXITCODE
