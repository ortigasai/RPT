<#
.SYNOPSIS
  Keep the server's C:\RPT identical to this dev laptop (code, config, samples).
  Run ON THE DEV LAPTOP whenever you've changed the app.

.DESCRIPTION
  robocopy /MIR of the project to the server, excluding the venv, git and logs.
  Optionally also mirrors the PaddleOCR models and the Tesseract vendor copy,
  and restarts the backend service so the change goes live.

.EXAMPLE
  # via admin share
  .\deploy\Sync-DevToServer.ps1 -ServerHost RPT-SVR01 -RestartService

  # via an explicit path (mapped drive / UNC)
  .\deploy\Sync-DevToServer.ps1 -Dest \\RPT-SVR01\c$\RPT -IncludeModels -RestartService
#>
[CmdletBinding(DefaultParameterSetName='Host')]
param(
  [Parameter(ParameterSetName='Host', Mandatory)][string]$ServerHost,
  [Parameter(ParameterSetName='Path', Mandatory)][string]$Dest,
  [string]$Source = (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
  [switch]$IncludeModels,
  [switch]$RestartService
)
$ErrorActionPreference = "Stop"
function Info ($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "[OK] $m" -ForegroundColor Green }

$Source = (Resolve-Path $Source).Path
if ($PSCmdlet.ParameterSetName -eq 'Host') { $Dest = "\\$ServerHost\c$\RPT" }
if (-not (Test-Path $Dest)) { throw "Destination not reachable: $Dest  (admin share access / credentials?)" }

Info "Mirroring project  $Source  ->  $Dest"
$xd = @(".venv","__pycache__",".git","logs","home","vendor","models","_work",".vscode",".idea") |
      ForEach-Object { Join-Path $Source $_ }
robocopy $Source $Dest /MIR /NFL /NDL /NJH /NJS /NP /R:2 /W:3 `
  /XD $xd `
  /XF "*.xlsx" "*.log" "RPT-migration-*.zip" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
Ok "project mirrored"

if ($IncludeModels) {
  $paddleSrc = Join-Path $env:USERPROFILE ".paddleocr"
  if (Test-Path $paddleSrc) {
    Info "Mirroring PaddleOCR models"
    robocopy $paddleSrc (Join-Path $Dest "home\.paddleocr") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    Ok "models mirrored"
  }
  $tess = @("$env:LOCALAPPDATA\Tesseract-OCR","C:\Program Files\Tesseract-OCR") |
          Where-Object { Test-Path (Join-Path $_ "tesseract.exe") } | Select-Object -First 1
  if ($tess) {
    Info "Mirroring Tesseract"
    robocopy $tess (Join-Path $Dest "vendor\Tesseract-OCR") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    Ok "Tesseract mirrored"
  }
}

if ($RestartService) {
  if ($PSCmdlet.ParameterSetName -eq 'Host') {
    Info "Restarting RPT-Backend on $ServerHost"
    try {
      Invoke-Command -ComputerName $ServerHost -ScriptBlock { Restart-Service RPT-Backend }
      Ok "service restarted"
    } catch {
      Write-Host "[!] Could not restart remotely ($($_.Exception.Message)). Run 'Restart-Service RPT-Backend' on the server." -ForegroundColor Yellow
    }
  } else {
    Write-Host "[!] -RestartService needs -ServerHost. Restart 'RPT-Backend' on the server manually." -ForegroundColor Yellow
  }
}
Ok "sync complete"
