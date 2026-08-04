#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
Write-Host "Windows service checks (idempotent)..."
# Docker Desktop manages its own services; ensure common services are noted.
$names = @('com.docker.service', 'docker')
foreach ($n in $names) {
  $s = Get-Service -Name $n -ErrorAction SilentlyContinue
  if ($s) {
    Write-Host "Service $n status=$($s.Status)"
    if ($s.Status -ne 'Running' -and $s.StartType -ne 'Disabled') {
      try { Start-Service $n -ErrorAction SilentlyContinue } catch { Write-Warning $_ }
    }
  }
}
Write-Host "GitHub runner: install as service via runnerdir\\svc.cmd after Register-GitHubRunner.ps1"
Write-Host "  cd C:\\iic-build\\runners\\actions-runner ; .\\svc.cmd install ; .\\svc.cmd start"
