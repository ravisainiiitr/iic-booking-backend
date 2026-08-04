#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
if (Get-Command pwsh -ErrorAction SilentlyContinue) {
  Write-Host "PowerShell 7 present: $(pwsh -NoLogo -Command '$PSVersionTable.PSVersion')"
  return
}
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Microsoft.PowerShell -e --accept-source-agreements --accept-package-agreements
} else {
  throw 'PowerShell 7 missing - install from https://aka.ms/powershell or via winget.'
}
