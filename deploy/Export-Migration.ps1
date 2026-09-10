<#
.SYNOPSIS
  Package this dev machine's RPT app + OCR models + Tesseract into one zip so
  the server can be brought up identical.  Run this ON THE DEV LAPTOP.

.DESCRIPTION
  Bundles:
    app\                 the project (no .venv / __pycache__ / .git / logs)
    app\broker_config.json  the live API key  (excluded from git, INCLUDED here)
    home\.paddleocr\     the downloaded PaddleOCR model weights
    vendor\Tesseract-OCR\ a copy of the local Tesseract install (self-contained)
    MANIFEST.json        versions + source host + timestamp

  Copy the resulting zip to the server and run deploy\Import-Migration.ps1.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy\Export-Migration.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy\Export-Migration.ps1 -OutFile D:\RPT-migration.zip
#>
[CmdletBinding()]
param(
  [string]$Source  = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
  [string]$OutFile = (Join-Path ([Environment]::GetFolderPath('Desktop')) `
                      ("RPT-migration-{0:yyyyMMdd-HHmm}.zip" -f (Get-Date)))
)
$ErrorActionPreference = "Stop"
function Info ($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "[OK] $m" -ForegroundColor Green }

$Source = (Resolve-Path $Source).Path
if (-not (Test-Path (Join-Path $Source "app.py"))) { throw "No app.py under $Source" }

$Stage = Join-Path $env:TEMP ("rpt-mig-" + [guid]::NewGuid().ToString("N"))
$AppStage    = Join-Path $Stage "app"
$HomeStage   = Join-Path $Stage "home\.paddleocr"
$VendorStage = Join-Path $Stage "vendor\Tesseract-OCR"
New-Item -ItemType Directory -Force -Path $AppStage,$HomeStage,$VendorStage | Out-Null

# ---- 1. project files -----------------------------------------------------
Info "Copying project from $Source"
$xd = @(".venv","__pycache__",".git","logs","_work") | ForEach-Object { Join-Path $Source $_ }
robocopy $Source $AppStage /E /NFL /NDL /NJH /NJS /NP `
  /XD $xd `
  /XF "*.xlsx" "*.log" "RPT-migration-*.zip" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
if (-not (Test-Path (Join-Path $AppStage "broker_config.json"))) {
  Write-Host "[!] broker_config.json not found - the server will have no API key" -ForegroundColor Yellow
}
Ok "project staged"

# ---- 2. PaddleOCR models -----------------------------------------------------
$PaddleSrc = Join-Path $env:USERPROFILE ".paddleocr"
if (Test-Path $PaddleSrc) {
  Info "Copying PaddleOCR models from $PaddleSrc"
  robocopy $PaddleSrc $HomeStage /E /NFL /NDL /NJH /NJS /NP | Out-Null
  Ok "models staged"
} else {
  Write-Host "[!] $PaddleSrc not found - run the app once so PaddleOCR downloads its models, or let the server download them on first start." -ForegroundColor Yellow
}

# ---- 3. Tesseract (optional fallback engine) --------------------------------
$TessDir = @(
  "$env:LOCALAPPDATA\Tesseract-OCR",
  "C:\Program Files\Tesseract-OCR",
  "C:\Program Files (x86)\Tesseract-OCR"
) | Where-Object { Test-Path (Join-Path $_ "tesseract.exe") } | Select-Object -First 1
if ($TessDir) {
  Info "Copying Tesseract from $TessDir"
  robocopy $TessDir $VendorStage /E /NFL /NDL /NJH /NJS /NP | Out-Null
  Ok "Tesseract staged"
} else {
  Remove-Item (Split-Path $VendorStage -Parent) -Recurse -Force
  Write-Host "[!] Tesseract not found locally - server will winget-install it (PaddleOCR is primary anyway)." -ForegroundColor Yellow
}

# ---- 4. manifest ---------------------------------------------------------
$pyver = ""
$venvPy = Join-Path $Source ".venv\Scripts\python.exe"
if (Test-Path $venvPy) { $pyver = (& $venvPy --version) 2>&1 }
@{
  created      = (Get-Date).ToString("o")
  source_host  = $env:COMPUTERNAME
  source_user  = $env:USERNAME
  source_path  = $Source
  python       = "$pyver"
  has_models   = [bool](Get-ChildItem $HomeStage -ErrorAction SilentlyContinue)
  has_tesseract= [bool]$TessDir
  has_broker_key = (Test-Path (Join-Path $AppStage "broker_config.json"))
  target       = "C:\RPT   (site :7272  ->  backend 127.0.0.1:7373, service RPT-Backend)"
} | ConvertTo-Json | Set-Content (Join-Path $Stage "MANIFEST.json") -Encoding UTF8

# ---- 5. zip ------------------------------------------------------------
Info "Compressing -> $OutFile  (this can take a minute)"
if (Test-Path $OutFile) { Remove-Item $OutFile -Force }
Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $OutFile -CompressionLevel Optimal
Remove-Item $Stage -Recurse -Force

$size = "{0:N1} MB" -f ((Get-Item $OutFile).Length / 1MB)
Ok "Bundle ready: $OutFile  ($size)"
Write-Host ""
Write-Host "Next:  copy that zip to the server, then run (elevated):" -ForegroundColor Cyan
Write-Host "   powershell -ExecutionPolicy Bypass -File <extracted>\deploy\Import-Migration.ps1 -Bundle C:\path\to\$(Split-Path $OutFile -Leaf)"
