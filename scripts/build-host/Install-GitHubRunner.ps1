#Requires -Version 5.1
param([string]$BuildRoot = 'C:\iic-build')
$ErrorActionPreference = 'Stop'
$runnerDir = Join-Path $BuildRoot 'runners\actions-runner'
if (Test-Path (Join-Path $runnerDir 'config.cmd')) {
  Write-Host "GitHub runner files present at $runnerDir"
  return
}
New-Item -ItemType Directory -Force -Path $runnerDir | Out-Null
Write-Host "Download Actions runner into $runnerDir (manual version pin recommended)."
Write-Host 'See: https://github.com/actions/runner/releases'
Write-Host 'Example:'
Write-Host "  cd $runnerDir"
Write-Host '  Invoke-WebRequest -Uri <runner-zip-url> -OutFile actions-runner.zip'
Write-Host '  Expand-Archive actions-runner.zip -DestinationPath .'
Write-Warning 'Install-GitHubRunner leaves download manual for version control - place runner binaries then run Register-GitHubRunner.ps1'
