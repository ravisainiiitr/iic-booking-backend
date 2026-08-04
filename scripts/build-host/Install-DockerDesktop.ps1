#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
if (Get-Command docker -ErrorAction SilentlyContinue) {
  Write-Host "Docker CLI present: $(docker --version)"
  return
}
$desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
if (Test-Path $desktop) {
  Write-Host 'Docker Desktop installed but docker not on PATH - start Docker Desktop and refresh PATH.'
  return
}
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Docker.DockerDesktop -e --accept-source-agreements --accept-package-agreements
  Write-Warning 'Reboot / start Docker Desktop, enable WSL2 backend, then re-run Verify-BuildHost.ps1'
} else {
  throw 'Install Docker Desktop from https://www.docker.com/products/docker-desktop/'
}
