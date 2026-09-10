<#
.SYNOPSIS  Remove the RPT IIS site and NSSM backend service (leaves C:\RPT files).
.EXAMPLE   powershell -ExecutionPolicy Bypass -File C:\RPT\deploy\Uninstall-Server.ps1
#>
[CmdletBinding()]
param(
  [string]$AppRoot     = "C:\RPT",
  [string]$ServiceName = "RPT-Backend",
  [string]$SiteName    = "RPT",
  [string]$AppPoolName = "RPT",
  [int]   $SitePort    = 7272
)
$ErrorActionPreference = "SilentlyContinue"
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Host "Run elevated." -ForegroundColor Red; exit 1
}

$Nssm = Join-Path $AppRoot "tools\nssm.exe"
if (Test-Path $Nssm) {
  & $Nssm stop   $ServiceName | Out-Null
  & $Nssm remove $ServiceName confirm | Out-Null
  Write-Host "[OK] service '$ServiceName' removed"
}

Import-Module WebAdministration
if (Test-Path "IIS:\Sites\$SiteName")     { Remove-Website   -Name $SiteName;    Write-Host "[OK] site '$SiteName' removed" }
if (Test-Path "IIS:\AppPools\$AppPoolName") { Remove-WebAppPool -Name $AppPoolName; Write-Host "[OK] app pool '$AppPoolName' removed" }

Get-NetFirewallRule -DisplayName "RPT HTTP $SitePort" | Remove-NetFirewallRule
Write-Host "[OK] firewall rule removed"
Write-Host "Files under $AppRoot were left in place."
