$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$python = Join-Path $root "venv\Scripts\python.exe"
$log = Join-Path $root "woosync.log"

if (-not (Test-Path $python)) {
    "venv not found at $python" | Out-File -Append -Encoding utf8 $log
    exit 1
}

& $python manage.py sync_woo 2>&1 | Out-File -Append -Encoding utf8 $log
$code = $LASTEXITCODE

# keep the log small
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 2MB)) {
    $keep = Get-Content $log -Tail 2000
    $keep | Set-Content -Encoding utf8 $log
}
exit $code