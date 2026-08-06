#Requires -RunAsAdministrator
<#
.SYNOPSIS
  R.3 clean-state wipe for Remote Analysis Agent (System B).
#>
$ErrorActionPreference = "Continue"
$log = Join-Path $env:TEMP ("r3-clean-raa-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
function L($m) { $line = "$(Get-Date -Format o) $m"; $line | Tee-Object -FilePath $log -Append }

L "=== R.3 RAA clean-state START ==="

$svc = "RemoteAnalysisAgent"
try {
  if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
    L "Stopping $svc"
    Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
    sc.exe delete $svc | Out-Null
    Start-Sleep -Seconds 3
  }
} catch { L "Service: $_" }

Get-Process -Name "RemoteAnalysisAgent*","RemoteAnalysisAgentSetup*" -ErrorAction SilentlyContinue |
  ForEach-Object { L "Kill $($_.ProcessName)"; Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }

$paths = @(
  "$env:ProgramData\RemoteAnalysisAgent",
  "C:\Services\RemoteAnalysisAgent",
  "$env:LOCALAPPDATA\RemoteAnalysisAgent",
  "$env:APPDATA\RemoteAnalysisAgent"
)
foreach ($p in $paths) {
  if (Test-Path $p) {
    L "Remove $p"
    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# Firewall rule for local health port (best-effort)
try {
  Get-NetFirewallRule -DisplayName "*Remote Analysis*" -ErrorAction SilentlyContinue |
    ForEach-Object { L "Remove firewall $($_.DisplayName)"; Remove-NetFirewallRule -Name $_.Name -ErrorAction SilentlyContinue }
} catch { L "Firewall: $_" }

L "=== R.3 RAA clean-state DONE === log=$log"
Write-Host "DONE. Log: $log"
Write-Host "Also revoke/replace prior Remote Analysis device in Portal before re-provision."
