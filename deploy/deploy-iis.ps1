# Deploys the RPT Assessment app on this Windows Server, the same shape as
# cwt-tax-portal:
#   - builds the Python venv from requirements.txt
#   - creates/updates the "RPT" PostgreSQL schema  (python -m rpt.db upgrade,
#     the analogue of cwt's `prisma migrate deploy`)
#   - warms the PaddleOCR model weights into C:\RPT\home\.paddleocr
#   - installs/updates an NSSM service running the Flask backend on port 7373
#   - creates/updates an IIS site on port 7272 whose web.config reverse-proxies
#     everything to the backend (the Flask app renders its own pages, so there
#     is no separate static client to serve)
#
# Prerequisites this script does NOT install for you (it warns if missing):
#   - Python 3.12 on PATH (or it will try `winget install Python.Python.3.12`)
#   - IIS with URL Rewrite + Application Request Routing (ARR)  -  the script
#     downloads and installs both from Microsoft if they're absent
#   - NSSM  -  bundled at tools\nssm.exe, or downloaded from nssm.cc
#   - A reachable PostgreSQL with a database named "RPT" (DATABASE_URL in .env)
#
# Usage (run as Administrator):
#   powershell -ExecutionPolicy Bypass -File C:\RPT\deploy\deploy-iis.ps1
#
# Safe to re-run  -  every step checks current state before changing it.

param(
    [string]$RepoRoot     = 'C:\RPT',
    [string]$SiteName     = 'RPT',
    [int]   $FrontendPort = 7272,
    [int]   $BackendPort  = 7373,
    [string]$ServiceName  = 'RptAssessmentServer',
    [string]$AppPoolName  = 'RPT',
    [string]$PythonExe    = '',
    [string]$NssmExe      = ''
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# --- Preflight ------------------------------------------------------------

Write-Step "Checking prerequisites"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in an ELEVATED PowerShell (Run as administrator)."
}
if (-not (Test-Path (Join-Path $RepoRoot 'app.py'))) {
    throw "app.py not found under $RepoRoot  -  clone/copy the repo there first (deploy\pull-latest.ps1)."
}
if (-not (Test-Path (Join-Path $RepoRoot '.env'))) {
    throw ".env is missing at $RepoRoot  -  copy .env.production.example to .env and fill it in (see deploy\DEPLOYMENT.md), or run deploy\migrate-data.ps1 from the dev laptop first."
}

$Venv    = Join-Path $RepoRoot '.venv'
$VenvPy  = Join-Path $Venv 'Scripts\python.exe'
$Logs    = Join-Path $RepoRoot 'logs'
$Home_   = Join-Path $RepoRoot 'home'
$Vendor  = Join-Path $RepoRoot 'vendor'
$IisRoot = Join-Path $RepoRoot 'iis'
$Tools   = Join-Path $RepoRoot 'tools'
New-Item -ItemType Directory -Force -Path $Logs,$Home_,$Tools,$IisRoot | Out-Null

# paddlepaddle / numpy only ship wheels up to CPython 3.12, so the venv MUST be
# built with 3.10-3.12 even if a newer Python is on PATH.
function Test-PySupported($exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    try {
        $v = & $exe -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        return ($v -match '^3\.(1[0-2]|[89])$')
    } catch { return $false }
}
function Find-Python {
    if ($PythonExe) {
        if (Test-PySupported $PythonExe) { return $PythonExe }
        throw "-PythonExe '$PythonExe' is not a supported version (need 3.10-3.12)."
    }
    $cands = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
        "C:\Python312\python.exe","C:\Python311\python.exe","C:\Python310\python.exe",
        "$env:ProgramFiles\Python312\python.exe","$env:ProgramFiles\Python311\python.exe")
    $cands += (Get-Command python.exe,python3.12.exe,python3.11.exe -ErrorAction SilentlyContinue |
               Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -ExpandProperty Source)
    try { $cands += (& py -3.12 -c "import sys;print(sys.executable)" 2>$null) } catch {}
    foreach ($c in $cands) { if (Test-PySupported $c) { return $c } }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Warn2 "No Python 3.10-3.12 found (paddlepaddle needs it)  -  installing 3.12 via winget"
    winget install --id Python.Python.3.12 -e --scope machine `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
    $py = Find-Python
}
if (-not $py) {
    throw "Need Python 3.10-3.12 (this server appears to have a newer one that paddlepaddle has no wheels for). Install Python 3.12 from python.org, then re-run  -  or re-run with -PythonExe C:\path\to\python312\python.exe"
}
$pyver = & $py -c "import sys;print('%d.%d.%d'%sys.version_info[:3])"
Write-Ok "Python: $py  (v$pyver)"

$rewriteDll = Join-Path $env:SystemRoot 'System32\inetsrv\rewrite.dll'
if (-not (Test-Path $rewriteDll)) { Write-Warn2 "IIS URL Rewrite not detected  -  the script will install it below." }
$arrDll = Join-Path ${env:ProgramFiles} 'IIS\Application Request Routing\requestRouter.dll'
if (-not (Test-Path $arrDll)) { Write-Warn2 "ARR not detected  -  the script will install it below." }

# --- NSSM --------------------------------------------------------------
function Resolve-Nssm {
    if ($NssmExe -and (Test-Path $NssmExe)) { return $NssmExe }
    if (Test-Path (Join-Path $Tools 'nssm.exe')) { return (Join-Path $Tools 'nssm.exe') }
    $onPath = (Get-Command nssm.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if ($onPath) { return $onPath }
    Write-Step "Downloading NSSM"
    $zip = Join-Path $env:TEMP 'nssm.zip'
    Invoke-WebRequest -UseBasicParsing 'https://nssm.cc/release/nssm-2.24.zip' -OutFile $zip
    $tmp = Join-Path $env:TEMP 'nssm_x'
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive $zip -DestinationPath $tmp -Force
    Copy-Item (Join-Path $tmp 'nssm-2.24\win64\nssm.exe') (Join-Path $Tools 'nssm.exe') -Force
    Remove-Item $zip,$tmp -Recurse -Force -ErrorAction SilentlyContinue
    return (Join-Path $Tools 'nssm.exe')
}
$nssm = Resolve-Nssm
Write-Ok "NSSM: $nssm"

# --- Stop the backend so its files aren't locked ------------------------
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne 'Stopped') {
    Write-Step "Stopping '$ServiceName' so the build can overwrite its files"
    & $nssm stop $ServiceName | Out-Null
    try { $svc.WaitForStatus('Stopped', (New-TimeSpan -Seconds 30)); Write-Ok "Stopped" }
    catch { throw "'$ServiceName' did not stop within 30s  -  stop it manually and re-run." }
}

# --- Build the venv --------------------------------------------------
Write-Step "Building the Python environment"
if ((Test-Path $VenvPy) -and -not (Test-PySupported $VenvPy)) {
    Write-Warn2 "Existing .venv was built with an unsupported Python  -  recreating"
    Remove-Item $Venv -Recurse -Force
}
if (-not (Test-Path $VenvPy)) { & $py -m venv $Venv }
& $VenvPy -m pip install --upgrade pip --quiet
& $VenvPy -m pip install -r (Join-Path $RepoRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    throw "pip install failed  -  see the errors above. Most likely the venv Python is unsupported (paddlepaddle needs 3.10-3.12) or the server has no internet access to PyPI."
}
& $VenvPy -m pip install --quiet setuptools
# sanity: the imports the app actually needs
& $VenvPy -c "import flask, sqlalchemy, dotenv, cv2, paddleocr, fitz" 2>$null
if ($LASTEXITCODE -ne 0) { throw "venv is missing core packages after pip install  -  check the pip output above." }
Write-Ok "venv ready ($pyver)"

# --- Database schema  (cwt's `prisma migrate deploy` analogue) ----------
Write-Step "Applying the database schema (RPT)"
Push-Location $RepoRoot
try {
    & $VenvPy -m rpt.db upgrade
    if ($LASTEXITCODE -ne 0) {
        Write-Warn2 "Schema step reported a problem  -  check DATABASE_URL in .env and that the 'RPT' database exists and is reachable. The app will still start (runs just won't be saved)."
    } else { Write-Ok "Schema is up to date" }
} finally { Pop-Location }

# --- Tesseract (optional fallback engine) ------------------------------
$tessCmd = ''
foreach ($c in @(
    (Join-Path $Vendor 'Tesseract-OCR\tesseract.exe'),
    'C:\Program Files\Tesseract-OCR\tesseract.exe',
    "$env:LOCALAPPDATA\Tesseract-OCR\tesseract.exe")) {
    if (Test-Path $c) { $tessCmd = $c; break }
}
if ($tessCmd) { Write-Ok "Tesseract: $tessCmd" } else { Write-Warn2 "No Tesseract  -  PaddleOCR only (fine)" }

# --- Warm the PaddleOCR models --------------------------------------
Write-Step "Warming PaddleOCR models into $Home_\.paddleocr"
$env:USERPROFILE = $Home_
Push-Location $RepoRoot
try {
    & $VenvPy -c "import sys; sys.path.insert(0,'.'); from rpt import ocr_paddle; print('warmup', ocr_paddle.warmup())"
} catch { Write-Warn2 "Model warm-up failed ($($_.Exception.Message))  -  the service will retry on first request." }
finally { Pop-Location }

# --- NSSM service ---------------------------------------------------
Write-Step "Installing/updating the backend service ('$ServiceName')  ->  127.0.0.1:$BackendPort"
if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    & $nssm install $ServiceName $VenvPy ('"' + (Join-Path $RepoRoot 'app.py') + '"')
    if ($LASTEXITCODE -ne 0) { throw "nssm install failed" }
    Write-Ok "Service installed"
} else { Write-Ok "Service exists  -  updating settings" }

& $nssm set $ServiceName AppDirectory   $RepoRoot
& $nssm set $ServiceName DisplayName    "RPT Assessment backend"
& $nssm set $ServiceName Description     "Flask backend for the RPT Assessment extractor (IIS site '$SiteName' reverse-proxies to it)."
& $nssm set $ServiceName Start           SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout      (Join-Path $Logs 'service-out.log')
& $nssm set $ServiceName AppStderr      (Join-Path $Logs 'service-err.log')
& $nssm set $ServiceName AppRotateFiles  1
& $nssm set $ServiceName AppRotateOnline 1
& $nssm set $ServiceName AppRotateBytes  10485760
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 3000

# app.py reads DATABASE_URL / RPT_BROKER_* / AUTH_* / RPT_PADDLE_MODELS / etc.
# from .env itself; the service only needs the few that pick the port + HOME.
$envExtra = @(
    "RPT_PORT=$BackendPort",
    "RPT_HOST=127.0.0.1",
    "PYTHONUNBUFFERED=1",
    "USERPROFILE=$Home_",
    "HOMEDRIVE=$($Home_.Substring(0,2))",
    "HOMEPATH=$($Home_.Substring(2))"
)
if ($tessCmd) { $envExtra += "TESSERACT_CMD=$tessCmd" }
& $nssm set $ServiceName AppEnvironmentExtra $envExtra

Start-Service $ServiceName
Write-Ok "Service '$ServiceName' running"

# --- web.config into C:\RPT\iis ------------------------------------
Write-Step "Writing web.config (backend port $BackendPort)"
$tpl = Get-Content (Join-Path $PSScriptRoot 'web.config') -Raw
$out = $tpl -replace '__BACKEND_PORT__', "$BackendPort"
[System.IO.File]::WriteAllText((Join-Path $IisRoot 'web.config'), $out, (New-Object System.Text.UTF8Encoding $false))
Write-Ok "web.config written"

# --- IIS + URL Rewrite + ARR --------------------------------------
Write-Step "IIS"
$isServer = (Get-CimInstance Win32_OperatingSystem).ProductType -ne 1
if ($isServer) {
    Install-WindowsFeature -Name Web-Server,Web-Common-Http,Web-Static-Content,`
        Web-Default-Doc,Web-Http-Errors,Web-Http-Logging,Web-Mgmt-Console,Web-Scripting-Tools `
        -ErrorAction Stop | Out-Null
} else {
    Enable-WindowsOptionalFeature -Online -All -NoRestart -FeatureName `
        IIS-WebServerRole,IIS-WebServer,IIS-CommonHttpFeatures,IIS-StaticContent,`
        IIS-DefaultDocument,IIS-HttpErrors,IIS-HttpLogging,IIS-RequestMonitor,`
        IIS-ManagementConsole,IIS-ManagementScriptingTools -ErrorAction Stop | Out-Null
}

function Ensure-Msi($displayLike, $url, $name) {
    $hit = Get-ItemProperty `
        HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*,`
        HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\* `
        -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like $displayLike }
    if ($hit) { Write-Ok "$name present"; return }
    Write-Step "Installing $name"
    $msi = Join-Path $env:TEMP $name
    Invoke-WebRequest -UseBasicParsing $url -OutFile $msi
    $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait -PassThru
    if ($p.ExitCode -notin 0,3010) { throw "$name install failed (exit $($p.ExitCode))" }
    Remove-Item $msi -Force -ErrorAction SilentlyContinue
    Write-Ok "$name installed"
}
Ensure-Msi '*URL Rewrite*' `
    'https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi' 'urlrewrite.msi'
Ensure-Msi '*Application Request Routing*' `
    'https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi' 'arr.msi'

Import-Module WebAdministration
Start-Service W3SVC -ErrorAction SilentlyContinue

try {
    Set-WebConfigurationProperty -pspath 'MACHINE/WEBROOT/APPHOST' `
        -filter 'system.webServer/proxy' -name 'enabled' -value 'True'
    Set-WebConfigurationProperty -pspath 'MACHINE/WEBROOT/APPHOST' `
        -filter 'system.webServer/proxy' -name 'preserveHostHeader' -value 'True'
    Write-Ok "ARR proxying enabled at the server level"
} catch {
    Write-Warn2 "Could not enable ARR proxying automatically ($($_.Exception.Message))  -  in IIS Manager, server node > Application Request Routing Cache > Server Proxy Settings > tick 'Enable proxy'."
}

Write-Step "Publishing site '$SiteName' on http://*:$FrontendPort"
if (Test-Path "IIS:\Sites\$SiteName")      { Remove-Website  -Name $SiteName }
if (Test-Path "IIS:\AppPools\$AppPoolName"){ Remove-WebAppPool -Name $AppPoolName }
New-WebAppPool -Name $AppPoolName | Out-Null
Set-ItemProperty "IIS:\AppPools\$AppPoolName" managedRuntimeVersion ''
Set-ItemProperty "IIS:\AppPools\$AppPoolName" startMode 'AlwaysRunning'
Set-ItemProperty "IIS:\AppPools\$AppPoolName" processModel.idleTimeout '00:00:00'
New-Website -Name $SiteName -PhysicalPath $IisRoot -ApplicationPool $AppPoolName `
    -Port $FrontendPort -IPAddress '*' -Force | Out-Null
Start-Website -Name $SiteName
icacls $IisRoot /grant "IIS_IUSRS:(OI)(CI)RX" /grant "IUSR:(OI)(CI)RX" | Out-Null
Write-Ok "IIS site '$SiteName' is up"

# --- Firewall -------------------------------------------------------
if (-not (Get-NetFirewallRule -DisplayName "RPT HTTP $FrontendPort" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "RPT HTTP $FrontendPort" -Direction Inbound `
        -Action Allow -Protocol TCP -LocalPort $FrontendPort | Out-Null
    Write-Ok "Firewall opened for TCP $FrontendPort"
}

# --- Health check --------------------------------------------------
Write-Step "Health check (PaddleOCR warm-up can take ~60 s on first start)"
function Wait-Http($url,$label,$timeout=150) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeout) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 8
            if ($r.StatusCode -eq 200) { Write-Ok "$label OK"; return $r.Content }
        } catch { Start-Sleep 3 }
    }
    Write-Warn2 "$label did not answer within $timeout s ($url)  -  see $Logs\service-err.log"
    return $null
}
$hc = Wait-Http "http://127.0.0.1:$BackendPort/health" "Backend /health" 150
if ($hc) { Write-Host "    $hc" -ForegroundColor DarkGray }
Wait-Http "http://127.0.0.1:$FrontendPort/health" "IIS reverse proxy" 60 | Out-Null

$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } |
       Select-Object -First 1 -ExpandProperty IPAddress)
Write-Host "`n$('=' * 64)" -ForegroundColor Green
Write-Host "  RPT Assessment deployed." -ForegroundColor Green
Write-Host "  On the server : http://localhost:$FrontendPort"
if ($ip) { Write-Host "  On the LAN    : http://$ip`:$FrontendPort" }
Write-Host "  Backend       : 127.0.0.1:$BackendPort   (service '$ServiceName')"
Write-Host "  Logs          : $Logs"
Write-Host "  Restart       : Restart-Service $ServiceName   (or: nssm restart $ServiceName)"
Write-Host "$('=' * 64)" -ForegroundColor Green
