<#
.SYNOPSIS
  One-shot IIS + NSSM deployment for the RPT Assessment app.

.DESCRIPTION
  Run this ON THE SERVER, in an elevated PowerShell, after the code is in
  place at C:\RPT (use deploy\Import-Migration.ps1 or a git clone).

  It will:
    1. create/refresh the Python venv from requirements.txt
    2. locate Tesseract (bundled vendor copy, system install, or winget)
    3. warm the PaddleOCR models into C:\RPT\home\.paddleocr
    4. register the Flask backend as the Windows service "RPT-Backend"
       (NSSM) listening on 127.0.0.1:7373
    5. install/enable IIS + URL Rewrite + ARR and publish the site "RPT"
       on http://*:7272, reverse-proxying to the backend
    6. open the firewall for TCP 7272 and health-check both tiers

  Safe to re-run; every step is idempotent.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\RPT\deploy\Deploy-Server.ps1
#>
[CmdletBinding()]
param(
  [string]$AppRoot     = "C:\RPT",
  [int]   $BackendPort = 7373,
  [int]   $SitePort    = 7272,
  [string]$ServiceName = "RPT-Backend",
  [string]$SiteName    = "RPT",
  [string]$AppPoolName = "RPT",
  [switch]$SkipDeps,
  [switch]$SkipService,
  [switch]$SkipIIS
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Info ($m) { Write-Host "[*] $m"      -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "[OK] $m"     -ForegroundColor Green }
function Warn ($m) { Write-Host "[!] $m"      -ForegroundColor Yellow }
function Die  ($m) { Write-Host "[X] $m"      -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- pre-flight
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Die "Run this in an ELEVATED PowerShell (Run as administrator)."
}
if (-not (Test-Path (Join-Path $AppRoot "app.py"))) {
  Die "app.py not found under $AppRoot - put the code there first."
}

$Venv      = Join-Path $AppRoot ".venv"
$VenvPy    = Join-Path $Venv "Scripts\python.exe"
$Logs      = Join-Path $AppRoot "logs"
$Home_     = Join-Path $AppRoot "home"          # HOME for the service (writable)
$Vendor    = Join-Path $AppRoot "vendor"
$IisRoot   = Join-Path $AppRoot "iis"
$Tools     = Join-Path $AppRoot "tools"
$Models    = Join-Path $AppRoot "models\paddleocr"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path

New-Item -ItemType Directory -Force -Path $Logs,$Home_,$Tools,$IisRoot | Out-Null

# ---------------------------------------------------------------- 1. python
function Find-Python {
  foreach ($c in @(
      "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
      "C:\Python312\python.exe","C:\Python311\python.exe")) {
    if (Test-Path $c) { return $c }
  }
  $p = (Get-Command python.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch "WindowsApps" } |
        Select-Object -First 1).Source
  if ($p) { return $p }
  return $null
}

if (-not $SkipDeps) {
  Info "Python environment"
  if (-not (Test-Path $VenvPy)) {
    $py = Find-Python
    if (-not $py) {
      Info "Installing Python 3.12 via winget ..."
      winget install --id Python.Python.3.12 -e --scope machine `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
      $py = Find-Python
    }
    if (-not $py) { Die "Python not found and could not be installed. Install Python 3.12 and re-run." }
    Info "Creating venv with $py"
    & $py -m venv $Venv
  }
  & $VenvPy -m pip install --upgrade pip --quiet
  & $VenvPy -m pip install --quiet -r (Join-Path $AppRoot "requirements.txt")
  & $VenvPy -m pip install --quiet setuptools
  Ok "venv ready: $VenvPy"
} else { Warn "SkipDeps - not touching the venv" }

# ---------------------------------------------------------------- 2. tesseract
$TessCmd = ""
foreach ($c in @(
    (Join-Path $Vendor "Tesseract-OCR\tesseract.exe"),
    "C:\Program Files\Tesseract-OCR\tesseract.exe",
    "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "$env:LOCALAPPDATA\Tesseract-OCR\tesseract.exe")) {
  if (Test-Path $c) { $TessCmd = $c; break }
}
if (-not $TessCmd -and -not $SkipDeps) {
  Info "Tesseract not found - trying winget (optional fallback engine)"
  try {
    winget install --id tesseract-ocr.tesseract -e `
      --accept-package-agreements --accept-source-agreements --disable-interactivity
    foreach ($c in @("C:\Program Files\Tesseract-OCR\tesseract.exe",
                     "$env:LOCALAPPDATA\Tesseract-OCR\tesseract.exe")) {
      if (Test-Path $c) { $TessCmd = $c; break }
    }
  } catch { Warn "Tesseract install skipped: $($_.Exception.Message)" }
}
if ($TessCmd) { Ok "Tesseract: $TessCmd" } else { Warn "No Tesseract - PaddleOCR only (fine)" }

# ---------------------------------------------------------------- 3. paddle models
$PaddleModelsEnv = ""
if ((Test-Path (Join-Path $Models "det")) -and (Test-Path (Join-Path $Models "rec"))) {
  $PaddleModelsEnv = $Models
  Ok "Bundled PaddleOCR models: $Models"
} elseif (-not $SkipDeps) {
  Info "Warming PaddleOCR models into $Home_\.paddleocr (one-time download) ..."
  $env:USERPROFILE = $Home_
  try {
    & $VenvPy -c "import sys; sys.path.insert(0,r'$AppRoot'); from rpt import ocr_paddle; print('warmup', ocr_paddle.warmup())"
    Ok "PaddleOCR models ready under $Home_\.paddleocr"
  } catch {
    Warn "Model warm-up failed ($($_.Exception.Message)); the service will retry on first request (needs internet once)."
  }
}

# ---------------------------------------------------------------- 4. NSSM service
$Nssm = Join-Path $Tools "nssm.exe"
if (-not (Test-Path $Nssm)) {
  Info "Downloading NSSM ..."
  $zip = Join-Path $env:TEMP "nssm.zip"
  Invoke-WebRequest -UseBasicParsing "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip
  $tmp = Join-Path $env:TEMP "nssm_extract"
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  Expand-Archive $zip -DestinationPath $tmp -Force
  Copy-Item (Join-Path $tmp "nssm-2.24\win64\nssm.exe") $Nssm -Force
  Remove-Item $zip,$tmp -Recurse -Force -ErrorAction SilentlyContinue
}
Ok "nssm: $Nssm"

if (-not $SkipService) {
  Info "Registering service '$ServiceName' -> Flask on 127.0.0.1:$BackendPort"
  $exists = (& $Nssm status $ServiceName) 2>$null
  if ($LASTEXITCODE -eq 0) { & $Nssm stop $ServiceName | Out-Null; & $Nssm remove $ServiceName confirm | Out-Null }

  & $Nssm install $ServiceName $VenvPy ("`"" + (Join-Path $AppRoot "app.py") + "`"")
  & $Nssm set $ServiceName AppDirectory  $AppRoot
  & $Nssm set $ServiceName DisplayName   "RPT Assessment backend"
  & $Nssm set $ServiceName Description    "Flask backend for the RPT Assessment extractor (reverse-proxied by IIS site '$SiteName')."
  & $Nssm set $ServiceName Start          SERVICE_AUTO_START
  & $Nssm set $ServiceName AppStdout     (Join-Path $Logs "backend.out.log")
  & $Nssm set $ServiceName AppStderr     (Join-Path $Logs "backend.err.log")
  & $Nssm set $ServiceName AppRotateFiles 1
  & $Nssm set $ServiceName AppRotateOnline 1
  & $Nssm set $ServiceName AppRotateBytes 10485760
  & $Nssm set $ServiceName AppExit Default Restart
  & $Nssm set $ServiceName AppRestartDelay 3000

  $envLines = @(
    "RPT_PORT=$BackendPort",
    "RPT_HOST=127.0.0.1",
    "RPT_OCR_BACKEND=paddle",
    "PYTHONUNBUFFERED=1",
    "USERPROFILE=$Home_",
    "HOMEDRIVE=$($Home_.Substring(0,2))",
    "HOMEPATH=$($Home_.Substring(2))"
  )
  if ($PaddleModelsEnv) { $envLines += "RPT_PADDLE_MODELS=$PaddleModelsEnv" }
  if ($TessCmd)         { $envLines += "TESSERACT_CMD=$TessCmd" }
  & $Nssm set $ServiceName AppEnvironmentExtra $envLines

  Start-Service $ServiceName
  Ok "Service '$ServiceName' started"
} else { Warn "SkipService - service not touched" }

# ---------------------------------------------------------------- 5. IIS
function Ensure-IisFeatures {
  $isServer = (Get-CimInstance Win32_OperatingSystem).ProductType -ne 1
  if ($isServer) {
    $feat = @("Web-Server","Web-Common-Http","Web-Static-Content","Web-Default-Doc",
              "Web-Http-Errors","Web-Http-Logging","Web-Request-Monitor",
              "Web-Mgmt-Console","Web-Scripting-Tools")
    Install-WindowsFeature -Name $feat -ErrorAction Stop | Out-Null
  } else {
    $opt = @("IIS-WebServerRole","IIS-WebServer","IIS-CommonHttpFeatures",
             "IIS-StaticContent","IIS-DefaultDocument","IIS-HttpErrors",
             "IIS-HttpLogging","IIS-RequestMonitor","IIS-ManagementConsole",
             "IIS-ManagementScriptingTools")
    Enable-WindowsOptionalFeature -Online -FeatureName $opt -All -NoRestart -ErrorAction Stop | Out-Null
  }
}

function Ensure-Msi ($displayLike, $url, $name) {
  $found = Get-ItemProperty HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*,
           HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\* -ErrorAction SilentlyContinue |
           Where-Object { $_.DisplayName -like $displayLike }
  if ($found) { Ok "$name already installed"; return }
  Info "Installing $name ..."
  $msi = Join-Path $env:TEMP $name
  Invoke-WebRequest -UseBasicParsing $url -OutFile $msi
  $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait -PassThru
  if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Die "$name install failed (exit $($p.ExitCode))" }
  Remove-Item $msi -Force -ErrorAction SilentlyContinue
  Ok "$name installed"
}

if (-not $SkipIIS) {
  Info "IIS features"
  Ensure-IisFeatures

  Ensure-Msi "*URL Rewrite*" `
    "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi" `
    "urlrewrite.msi"
  Ensure-Msi "*Application Request Routing*" `
    "https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi" `
    "arr.msi"

  Import-Module WebAdministration -ErrorAction Stop
  Start-Service W3SVC -ErrorAction SilentlyContinue

  Info "Enabling ARR reverse-proxy at server level"
  & "$env:windir\system32\inetsrv\appcmd.exe" set config `
    -section:system.webServer/proxy /enabled:"True" /preserveHostHeader:"True" /commit:apphost | Out-Null

  Info "Publishing site '$SiteName' on http://*:$SitePort  ->  127.0.0.1:$BackendPort"
  Copy-Item (Join-Path $DeployDir "web.config") (Join-Path $IisRoot "web.config") -Force

  if (Test-Path "IIS:\Sites\$SiteName")     { Remove-Website -Name $SiteName }
  if (Test-Path "IIS:\AppPools\$AppPoolName") { Remove-WebAppPool -Name $AppPoolName }

  New-WebAppPool -Name $AppPoolName | Out-Null
  Set-ItemProperty "IIS:\AppPools\$AppPoolName" managedRuntimeVersion ""
  Set-ItemProperty "IIS:\AppPools\$AppPoolName" startMode "AlwaysRunning"
  Set-ItemProperty "IIS:\AppPools\$AppPoolName" processModel.idleTimeout "00:00:00"

  New-Website -Name $SiteName -PhysicalPath $IisRoot -ApplicationPool $AppPoolName `
    -Port $SitePort -Force | Out-Null
  Start-Website -Name $SiteName

  icacls $IisRoot /grant "IIS_IUSRS:(OI)(CI)RX" /grant "IUSR:(OI)(CI)RX" | Out-Null
  Ok "IIS site '$SiteName' is up"
} else { Warn "SkipIIS - IIS not touched" }

# ---------------------------------------------------------------- 6. firewall + health
Info "Firewall rule for TCP $SitePort"
if (-not (Get-NetFirewallRule -DisplayName "RPT HTTP $SitePort" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "RPT HTTP $SitePort" -Direction Inbound `
    -Action Allow -Protocol TCP -LocalPort $SitePort | Out-Null
}

function Wait-Http ($url, $label, $timeoutSec = 120) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
    try {
      $r = Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 8
      if ($r.StatusCode -eq 200) { Ok "$label OK ($url)"; return $true }
    } catch { Start-Sleep 3 }
  }
  Warn "$label did not answer within $timeoutSec s ($url) - check $Logs"
  return $false
}

Info "Health checks (PaddleOCR warm-up can take ~60 s on first start)"
Wait-Http "http://127.0.0.1:$BackendPort/" "Backend service" 150 | Out-Null
Wait-Http "http://127.0.0.1:$SitePort/"    "IIS reverse proxy" 60  | Out-Null

$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" } |
       Select-Object -First 1 -ExpandProperty IPAddress)
Write-Host ""
Write-Host ("=" * 64) -ForegroundColor Green
Write-Host "  RPT Assessment deployed." -ForegroundColor Green
Write-Host "  On the server : http://localhost:$SitePort"
if ($ip) { Write-Host "  On the LAN    : http://$ip`:$SitePort" }
Write-Host "  Backend (internal): 127.0.0.1:$BackendPort   service '$ServiceName'"
Write-Host "  Logs          : $Logs"
Write-Host "  Restart backend : nssm restart $ServiceName   (or: Restart-Service $ServiceName)"
Write-Host ("=" * 64) -ForegroundColor Green
