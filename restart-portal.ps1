# Restarts the RPT Assessment app locally on this dev laptop. Safe to
# double-click ("Run with PowerShell") whenever the app stops responding
# after this machine sleeps/restarts.
#
# Unlike cwt-tax-portal there's no local database or separate client here -
# the PostgreSQL "RPT" database is on the tax server (see DATABASE_URL in
# .env) and the Flask app serves its own pages - so this just (re)starts
# the one process.
#
# Usage: right-click -> "Run with PowerShell", or:
#   powershell -ExecutionPolicy Bypass -File restart-portal.ps1

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot
$Venv     = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $Venv)) {
    Write-Host "No .venv yet - creating it and installing requirements..." -ForegroundColor Yellow
    $py = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        (Get-Command python.exe -ErrorAction SilentlyContinue |
         Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -First 1 -ExpandProperty Source)
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $py) { throw "Python 3.12 not found - install it first." }
    & $py -m venv (Join-Path $RepoRoot '.venv')
    & $Venv -m pip install --upgrade pip
    & $Venv -m pip install -r (Join-Path $RepoRoot 'requirements.txt')
}

Write-Host 'Stopping any running RPT app...'
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'app\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

Write-Host 'Starting RPT app...'
Start-Process powershell -ArgumentList @(
    '-NoExit', '-Command',
    "Set-Location '$RepoRoot'; & '$Venv' app.py"
)

Start-Sleep -Seconds 3
Write-Host ''
Write-Host 'A new PowerShell window opened for the app - leave it running.' -ForegroundColor Yellow
Write-Host 'Portal: http://localhost:5000' -ForegroundColor Green
