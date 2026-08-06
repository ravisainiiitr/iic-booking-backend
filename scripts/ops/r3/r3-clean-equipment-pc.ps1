#Requires -RunAsAdministrator
<#
.SYNOPSIS
  R.3 clean-state wipe for Equipment PC Configuration Wizard (Systems A / D).
#>
$ErrorActionPreference = "Continue"
$log = Join-Path $env:TEMP ("r3-clean-epc-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
function L($m) { $line = "$(Get-Date -Format o) $m"; $line | Tee-Object -FilePath $log -Append }

L "=== R.3 Equipment PC clean-state START ==="

Get-Process -Name "EquipmentPcConfigurationWizard*","EquipmentPc*" -ErrorAction SilentlyContinue |
  ForEach-Object { L "Kill $($_.ProcessName)"; Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }

$paths = @(
  "$env:ProgramData\DepartmentSyncAgent\Equipment",
  "$env:ProgramData\EquipmentPcConfigurationWizard",
  "$env:ProgramData\DepartmentSyncAgent\InstallerStaging",
  "$env:LOCALAPPDATA\EquipmentPcConfigurationWizard",
  "$env:APPDATA\EquipmentPcConfigurationWizard"
)
foreach ($p in $paths) {
  if (Test-Path $p) {
    L "Remove $p"
    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# SMB shares commonly created by wizard (best-effort; ignore missing)
Get-SmbShare -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match "Results|Raw|Equipment" -or $_.Path -like "*DepartmentSyncAgent\Equipment*" } |
  ForEach-Object {
    L "Remove share $($_.Name)"
    Remove-SmbShare -Name $_.Name -Force -ErrorAction SilentlyContinue
  }

L "=== R.3 Equipment PC clean-state DONE === log=$log"
Write-Host "DONE. Log: $log"
Write-Host "Also revoke/replace prior Equipment PC device in Portal before re-provision."
