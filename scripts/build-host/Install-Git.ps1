#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
if (Get-Command git -ErrorAction SilentlyContinue) {
  Write-Host "Git already installed: $(git --version)"
  return
}
Write-Host 'Install Git for Windows (winget if available)'
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
} else {
  throw 'Git missing and winget unavailable - install Git manually, then re-run.'
}
