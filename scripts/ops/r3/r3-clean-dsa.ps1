#Requires -RunAsAdministrator
<#
.SYNOPSIS
  R.3 clean-state wipe for Department Sync Agent (System C).
.NOTES
  Removes services, ProgramData, DPAPI staging, common installer leftovers.
  Does not uninstall .NET runtime.
#>
$ErrorActionPreference = "Continue"
$log = Join-Path $env:TEMP ("r3-clean-dsa-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
function L($m) { $line = "$(Get-Date -Format o) $m"; $line | Tee-Object -FilePath $log -Append }

L "=== R.3 DSA clean-state START ==="

$svcNames = @("DepartmentSyncAgent", "IIC.DepartmentSyncAgent")
foreach ($n in $svcNames) {
  try {
    $s = Get-Service -Name $n -ErrorAction SilentlyContinue
    if ($s) {
      L "Stopping service $n"
      Stop-Service -Name $n -Force -ErrorAction SilentlyContinue
      sc.exe delete $n | Out-Null
      Start-Sleep -Seconds 2
    }
  } catch { L "Service $n: $_" }
}

Get-Process -Name "DepartmentSyncAgent*","DepartmentSyncAgent.Installer*" -ErrorAction SilentlyContinue |
  ForEach-Object { L "Kill $($_.ProcessName) PID $($_.Id)"; Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }

$paths = @(
  "$env:ProgramData\DepartmentSyncAgent",
  "$env:ProgramFiles\DepartmentSyncAgent",
  "${env:ProgramFiles(x86)}\DepartmentSyncAgent",
  "C:\Services\DepartmentSyncAgent"
)
foreach ($p in $paths) {
  if (Test-Path $p) {
    L "Remove $p"
    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# Common AppData leftovers
@(
  "$env:LOCALAPPDATA\DepartmentSyncAgent",
  "$env:APPDATA\DepartmentSyncAgent"
) | ForEach-Object {
  if (Test-Path $_) { L "Remove $_"; Remove-Item $_ -Recurse -Force -ErrorAction SilentlyContinue }
}

L "=== R.3 DSA clean-state DONE === log=$log"
Write-Host "DONE. Log: $log"
