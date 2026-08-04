#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
if (Get-Command node -ErrorAction SilentlyContinue) {
  $v = (node -v)
  Write-Host "Node present: $v"
  if ($v -notmatch '^v20\.') { Write-Warning "Node 20 LTS recommended (found $v)" }
  return
}
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
} else {
  throw 'Install Node.js 20 LTS from https://nodejs.org/'
}
