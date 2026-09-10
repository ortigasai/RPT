# Run on THIS DEV LAPTOP (not the server). Makes the server's C:\RPT identical
# to this machine  -  same code, same .env, same OCR models, same Tesseract,
# and a fresh dump of the "RPT" PostgreSQL database.
#
#   1. pg_dump the database in .env's DATABASE_URL
#   2. robocopy the project (minus .venv / .git / logs) to \\<server>\c$\RPT
#   3. copy .env and broker_config.json over (secrets  -  they travel here,
#      not in git), patching RPT_PORT / RPT_HOST to the server's values
#   4. robocopy the PaddleOCR model weights (~/.paddleocr) and the local
#      Tesseract install to C:\RPT\home\.paddleocr and C:\RPT\vendor
#   5. copy the .dump to C:\RPT\migration\ and print a ready-to-paste
#      pg_restore command to run ON the server
#
# It does NOT restore the dump into the server's Postgres for you (that would
# overwrite live data), and it never handles the DB password  -  pg_restore
# prompts for it.  If the dev laptop and the server point at the SAME Postgres
# (both 192.168.0.215/RPT), the dump is just a backup and no restore is needed.
#
# Usage (from the repo root on this laptop):
#   powershell -ExecutionPolicy Bypass -File deploy\migrate-data.ps1 -ServerHost <server-hostname-or-ip>
#
# Requires an admin share (\\<ServerHost>\c$) reachable from this laptop, or
# pass -DestRoot with a UNC path / mapped drive you already have write access to.

param(
    [Parameter(Mandatory = $true)][string]$ServerHost,
    [string]$DestRoot   = "\\$ServerHost\c`$\RPT",

    [string]$PgDumpExe  = '',                 # auto-detected if blank
    # The SERVER's Postgres  -  only used to print the restore command.
    # Never put the password here; this file is committed to git.
    [string]$DestDbHost = '192.168.0.215',
    [string]$DestDbPort = '5432',
    [string]$DestDbUser = 'postgres',
    [string]$DestDbName = 'RPT',

    [int]   $ServerBackendPort = 7373,
    [switch]$SkipDbDump
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($m)  { Write-Host "    $m"   -ForegroundColor Yellow }

# --- read DATABASE_URL from .env ---------------------------------------
$envFile = Join-Path $RepoRoot '.env'
if (-not (Test-Path $envFile)) { throw ".env not found at $envFile" }
$envLines = Get-Content $envFile
function Get-EnvValue($name) {
    $l = $envLines | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -First 1
    if (-not $l) { return $null }
    return ($l -split '=', 2)[1].Trim()
}
$dbUrl = Get-EnvValue 'DATABASE_URL'
if (-not $dbUrl) { throw "DATABASE_URL not set in $envFile" }

# postgresql://user:pass@host:port/dbname  (pass may be %-encoded)
$m = [regex]::Match($dbUrl, '^\w+(?:\+\w+)?://([^:/@]+)(?::([^@]*))?@([^:/]+)(?::(\d+))?/([^?]+)')
if (-not $m.Success) { throw "Could not parse DATABASE_URL: $dbUrl" }
$srcUser = $m.Groups[1].Value
$srcPass = [uri]::UnescapeDataString($m.Groups[2].Value)
$srcHost = $m.Groups[3].Value
$srcPort = if ($m.Groups[4].Success) { $m.Groups[4].Value } else { '5432' }
$srcDb   = $m.Groups[5].Value
Write-Ok "Source DB: $srcUser@$srcHost`:$srcPort/$srcDb"

if (-not (Test-Path $DestRoot)) {
    throw "Can't reach '$DestRoot' from this laptop  -  confirm the server hostname/IP, that its C`$ admin share is reachable, and that your account has write access. Or pass -DestRoot with a path you do have access to."
}

# --- 1. dump the database ---------------------------------------------
$outputDir = Join-Path $PSScriptRoot '_migration-output'
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$dumpFile = Join-Path $outputDir ("RPT_$(Get-Date -Format 'yyyyMMdd_HHmmss').dump")

if ($SkipDbDump) {
    Write-Warn2 "Skipping DB dump (-SkipDbDump)"
} else {
    if (-not $PgDumpExe) {
        $PgDumpExe = @(
            'C:\Users\villegaskmp\portable-dev\pgsql\bin\pg_dump.exe',
            "$env:ProgramFiles\PostgreSQL\18\bin\pg_dump.exe",
            "$env:ProgramFiles\PostgreSQL\17\bin\pg_dump.exe",
            "$env:ProgramFiles\PostgreSQL\16\bin\pg_dump.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $PgDumpExe) { $PgDumpExe = (Get-Command pg_dump.exe -ErrorAction SilentlyContinue).Source }
    }
    if (-not $PgDumpExe) { throw "pg_dump.exe not found  -  pass -PgDumpExe <path>, or -SkipDbDump if you only want the files copied." }

    Write-Step "Dumping '$srcDb' from $srcHost`:$srcPort"
    $env:PGPASSWORD = $srcPass
    try {
        & $PgDumpExe -h $srcHost -p $srcPort -U $srcUser -Fc -f $dumpFile $srcDb
        if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
    } finally { Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue }
    Write-Ok "Dumped to $dumpFile ($([math]::Round((Get-Item $dumpFile).Length / 1MB, 1)) MB)"
}

# --- 2. project files ------------------------------------------------
# Includes .git so C:\RPT on the server is a true mirror of the repo (and
# `git pull` works there later). Excludes only the rebuilt/server-generated
# trees and the local venv.
Write-Step "Mirroring project (incl. .git)  ->  $DestRoot"
$xd = @('.venv','__pycache__','logs','home','vendor','models','iis','_work','_migration-output','.vscode','.idea') |
      ForEach-Object { Join-Path $RepoRoot $_ }
robocopy $RepoRoot $DestRoot /MIR /R:2 /W:3 /NFL /NDL /NJH /NJS /NP `
    /XD $xd `
    /XF '*.log' 'RPT-migration-*.zip' | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy of the project failed ($LASTEXITCODE)" }
Write-Ok "project mirrored"

# --- 3. secrets (.env, broker_config.json) --------------------------
Write-Step "Copying .env / broker_config.json (secrets  -  not in git)"
$destEnv = Join-Path $DestRoot '.env'
Copy-Item $envFile $destEnv -Force
# server-specific overrides (ports; HOME for the NSSM service so PaddleOCR's
# ~/.paddleocr cache - which we mirror to C:\RPT\home\.paddleocr below - is
# what it reads. deploy-iis.ps1 also sets USERPROFILE on the service itself.)
$serverEnv = Get-Content $destEnv
$serverEnv = $serverEnv -replace '^\s*RPT_PORT\s*=.*', "RPT_PORT=$ServerBackendPort"
$serverEnv = $serverEnv -replace '^\s*RPT_HOST\s*=.*', 'RPT_HOST=127.0.0.1'
if ($serverEnv -notmatch '^\s*USERPROFILE\s*=') { $serverEnv += 'USERPROFILE=C:/RPT/home' }
Set-Content $destEnv $serverEnv -Encoding UTF8
Write-Ok "C:\RPT\.env written (RPT_PORT=$ServerBackendPort, RPT_HOST=127.0.0.1)  -  review it on the server"

$brokerCfg = Join-Path $RepoRoot 'broker_config.json'
if (Test-Path $brokerCfg) { Copy-Item $brokerCfg (Join-Path $DestRoot 'broker_config.json') -Force; Write-Ok "broker_config.json copied" }

# --- 4. OCR models + Tesseract -------------------------------------
$paddleSrc = Join-Path $env:USERPROFILE '.paddleocr'
if (Test-Path $paddleSrc) {
    Write-Step "Mirroring PaddleOCR models"
    robocopy $paddleSrc (Join-Path $DestRoot 'home\.paddleocr') /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    Write-Ok "models mirrored  ->  C:\RPT\home\.paddleocr"
} else { Write-Warn2 "$paddleSrc not found  -  run the app once so PaddleOCR downloads its models, or let the server download them on first start" }

$tessDir = @("$env:LOCALAPPDATA\Tesseract-OCR",'C:\Program Files\Tesseract-OCR') |
           Where-Object { Test-Path (Join-Path $_ 'tesseract.exe') } | Select-Object -First 1
if ($tessDir) {
    Write-Step "Mirroring Tesseract"
    robocopy $tessDir (Join-Path $DestRoot 'vendor\Tesseract-OCR') /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    Write-Ok "Tesseract mirrored  ->  C:\RPT\vendor\Tesseract-OCR"
} else { Write-Warn2 "Tesseract not found locally  -  deploy-iis.ps1 will winget-install it (PaddleOCR is primary anyway)" }

# --- 5. copy the dump, print next steps ---------------------------
if (-not $SkipDbDump) {
    Write-Step "Copying the dump to the server"
    $destMig = Join-Path $DestRoot 'migration'
    New-Item -ItemType Directory -Force -Path $destMig | Out-Null
    Copy-Item $dumpFile $destMig
    $dumpName = Split-Path -Leaf $dumpFile
    Write-Ok "copied to C:\RPT\migration\$dumpName"
}

Write-Host "`nData copied. On the SERVER:" -ForegroundColor Yellow
if (-not $SkipDbDump) {
    if ($srcHost -eq $DestDbHost -and $srcDb -eq $DestDbName) {
        Write-Host "  * The dev laptop and the server use the SAME Postgres ($DestDbHost/$DestDbName)  -" -ForegroundColor White
        Write-Host "    no restore needed. C:\RPT\migration\$dumpName is kept as a backup." -ForegroundColor White
    } else {
        Write-Host "  1. Restore the database (prompts for the postgres password):" -ForegroundColor White
        Write-Host "       pg_restore --clean --if-exists -h $DestDbHost -p $DestDbPort -U $DestDbUser -d $DestDbName ""C:\RPT\migration\$dumpName""" -ForegroundColor White
    }
}
Write-Host "  2. Review C:\RPT\.env  (DATABASE_URL, RPT_BROKER_KEY, AUTH_* carried over from dev;" -ForegroundColor White
Write-Host "     RPT_PORT/RPT_HOST already set for the server)." -ForegroundColor White
Write-Host "  3. powershell -ExecutionPolicy Bypass -File C:\RPT\deploy\deploy-iis.ps1" -ForegroundColor White
