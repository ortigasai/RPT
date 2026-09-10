<#
.SYNOPSIS
  Unpack a migration bundle from Export-Migration.ps1 onto this server at
  C:\RPT, then run the IIS + NSSM deployment.  Run ELEVATED on the server.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\Import-Migration.ps1 -Bundle C:\Temp\RPT-migration-20260910-1400.zip
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$Bundle,
  [string]$AppRoot = "C:\RPT",
  [switch]$NoDeploy          # only lay down files, skip Deploy-Server.ps1
)
$ErrorActionPreference = "Stop"
function Info ($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "[OK] $m" -ForegroundColor Green }

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Run this ELEVATED (Run as administrator)." -ForegroundColor Red; exit 1
}
if (-not (Test-Path $Bundle)) { throw "Bundle not found: $Bundle" }

$Work = Join-Path $env:TEMP ("rpt-imp-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Work | Out-Null
Info "Extracting $Bundle"
Expand-Archive -Path $Bundle -DestinationPath $Work -Force

if (Test-Path (Join-Path $Work "MANIFEST.json")) {
  Write-Host "--- bundle manifest ---" -ForegroundColor DarkGray
  Get-Content (Join-Path $Work "MANIFEST.json") | Write-Host
  Write-Host "-----------------------" -ForegroundColor DarkGray
}

# stop the service so files aren't locked (ignore if not yet installed)
$svc = Get-Service "RPT-Backend" -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") { Info "Stopping RPT-Backend"; Stop-Service RPT-Backend }

New-Item -ItemType Directory -Force -Path $AppRoot,(Join-Path $AppRoot "logs") | Out-Null

Info "Laying down project files -> $AppRoot"
# /MIR keeps the tree identical to dev, but never wipe logs
robocopy (Join-Path $Work "app") $AppRoot /MIR /NFL /NDL /NJH /NJS /NP `
  /XD (Join-Path $AppRoot "logs") (Join-Path $AppRoot ".venv") | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy (app) failed ($LASTEXITCODE)" }
Ok "project files in place"

$homeSrc = Join-Path $Work "home\.paddleocr"
if (Test-Path $homeSrc) {
  Info "Restoring PaddleOCR models -> $AppRoot\home\.paddleocr"
  robocopy $homeSrc (Join-Path $AppRoot "home\.paddleocr") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
  Ok "models restored"
}

$vendorSrc = Join-Path $Work "vendor\Tesseract-OCR"
if (Test-Path $vendorSrc) {
  Info "Restoring Tesseract -> $AppRoot\vendor\Tesseract-OCR"
  robocopy $vendorSrc (Join-Path $AppRoot "vendor\Tesseract-OCR") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
  Ok "Tesseract restored"
}

Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue

if ($NoDeploy) {
  Ok "Files staged at $AppRoot. Run deploy\Deploy-Server.ps1 when ready."
  return
}

$deploy = Join-Path $AppRoot "deploy\Deploy-Server.ps1"
if (-not (Test-Path $deploy)) { throw "Deploy-Server.ps1 missing under $AppRoot\deploy" }
Info "Handing off to Deploy-Server.ps1"
& powershell -ExecutionPolicy Bypass -File $deploy -AppRoot $AppRoot
