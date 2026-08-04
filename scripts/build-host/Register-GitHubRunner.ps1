#Requires -Version 5.1
param(
    [string]$BuildRoot = 'C:\iic-build',
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Token,
    [string]$Name = 'iic-build-win',
    [string]$Labels = 'self-hosted,windows,iic-build'
)
$ErrorActionPreference = 'Stop'
$runnerDir = Join-Path $BuildRoot 'runners\actions-runner'
$config = Join-Path $runnerDir 'config.cmd'
if (-not (Test-Path $config)) {
  throw "Runner not installed at $runnerDir - run Install-GitHubRunner.ps1 first"
}
$creds = Join-Path $runnerDir '.credentials'
if (Test-Path $creds) {
  Write-Host 'Runner already configured (credentials exist) - skip register'
  return
}
Push-Location $runnerDir
try {
  & .\config.cmd --url $Url --token $Token --name $Name --labels $Labels --work '_work' --unattended --replace
} finally {
  Pop-Location
}
