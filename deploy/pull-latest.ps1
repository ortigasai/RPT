# Run ON THE SERVER. Gets the latest code onto it - clones the repo the first
# time, pulls on every run after. Doesn't touch data (.env, logs, the bundled
# OCR models / Tesseract, the Postgres DB) - none of that is in git.
#
# Usage (as Administrator, or any account with write access to $RepoRoot):
#   powershell -ExecutionPolicy Bypass -File deploy\pull-latest.ps1
#
# After this, run deploy-iis.ps1 to build and (re)deploy.

param(
    [string]$RepoRoot = 'C:\RPT',
    [string]$RepoUrl  = 'https://github.com/ortigasai/RPT.git',
    [string]$Branch   = 'main'
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    $msg" -ForegroundColor Yellow }

try { git --version | Out-Null } catch { throw "git not found on PATH - install Git for Windows first." }

if (Test-Path (Join-Path $RepoRoot '.git')) {
    Write-Step "Repo already present at $RepoRoot - pulling latest '$Branch'"
    Push-Location $RepoRoot
    try {
        & git fetch origin;            if ($LASTEXITCODE -ne 0) { throw "git fetch failed" }
        & git checkout $Branch;        if ($LASTEXITCODE -ne 0) { throw "git checkout $Branch failed" }
        & git pull origin $Branch;     if ($LASTEXITCODE -ne 0) { throw "git pull failed" }
    } finally { Pop-Location }
} elseif ((Test-Path $RepoRoot) -and ((Get-ChildItem $RepoRoot -Force | Measure-Object).Count -gt 0)) {
    throw "$RepoRoot exists and is non-empty but isn't a git repo (no .git folder) - clear it out first, or pass -RepoRoot pointing somewhere empty."
} else {
    Write-Step "Cloning into $RepoRoot"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $RepoRoot) -ErrorAction SilentlyContinue | Out-Null
    & git clone -b $Branch $RepoUrl $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw "git clone failed" }
}

$commit = (& git -C $RepoRoot rev-parse --short HEAD)
Write-Ok "Now at commit $commit on '$Branch'"

if (-not (Test-Path (Join-Path $RepoRoot '.env'))) {
    Write-Warn2 ".env doesn't exist yet - copy .env.production.example to .env and fill it in before running deploy-iis.ps1 (see deploy\DEPLOYMENT.md)."
} else {
    Write-Ok ".env already present"
}

Write-Host "`nNext: powershell -ExecutionPolicy Bypass -File deploy\deploy-iis.ps1" -ForegroundColor Green
