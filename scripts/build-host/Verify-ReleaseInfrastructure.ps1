#Requires -Version 5.1
param([switch]$TryEcrLogin)
$ErrorActionPreference = 'Stop'
& "$PSScriptRoot\Verify-BuildHost.ps1"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
  Write-Warning 'AWS CLI missing - skip AWS checks'
  exit 0
}

Write-Host 'AWS identity:'
aws sts get-caller-identity

if ($TryEcrLogin) {
  $reg = $env:ECR_REGISTRY
  $region = if ($env:AWS_REGION) { $env:AWS_REGION } else { 'ap-south-1' }
  if (-not $reg) { throw 'Set ECR_REGISTRY for login test' }
  aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $reg
  docker logout $reg
  Write-Host 'ECR login/logout OK'
}

$runnerDir = 'C:\iic-build\runners\actions-runner'
if (Test-Path (Join-Path $runnerDir '.runner')) {
  Write-Host 'PASS runner config present'
} else {
  Write-Warning 'Runner not configured yet'
}
