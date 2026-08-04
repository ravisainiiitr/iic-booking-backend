#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
if (Get-Command aws -ErrorAction SilentlyContinue) {
  Write-Host "AWS CLI present: $(aws --version)"
  return
}
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Amazon.AWSCLI -e --accept-source-agreements --accept-package-agreements
} else {
  throw 'Install AWS CLI v2 from https://aws.amazon.com/cli/'
}
