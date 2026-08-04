#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
Write-Host "Configuring Docker expectations for IIC build host..."
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Warning "docker not on PATH — start Docker Desktop after install"
  return
}
$env:DOCKER_BUILDKIT = '1'
[Environment]::SetEnvironmentVariable('DOCKER_BUILDKIT', '1', 'Machine')
try {
  docker info | Out-Null
  Write-Host "Docker engine reachable"
} catch {
  Write-Warning "Docker engine not reachable yet — launch Docker Desktop (Linux containers)"
}
Write-Host "Ensure: Linux containers mode, disk image >= 100GB, BuildKit enabled"
