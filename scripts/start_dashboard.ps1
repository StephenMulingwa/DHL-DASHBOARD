# Start exactly one DHL dashboard instance (kills stale flask_app.py processes first).
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'DHL-DASHBOARD.*flask_app\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Start-Sleep -Seconds 2

$venv = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venv)) {
    Write-Error "Missing $venv — create the venv first."
    exit 1
}

Write-Host "Starting DHL Fleet Health (Flask) at http://127.0.0.1:8050/login"
& $venv flask_app.py
