#Requires -Version 5.1
param([string]$BuildRoot = 'C:\iic-build')
$ErrorActionPreference = 'Stop'
$dirs = @(
  $BuildRoot,
  "$BuildRoot\runners",
  "$BuildRoot\repos",
  "$BuildRoot\artifacts\rc1\images",
  "$BuildRoot\artifacts\rc1\installers\dsa",
  "$BuildRoot\artifacts\rc1\installers\wizard",
  "$BuildRoot\artifacts\rc1\installers\raa",
  "$BuildRoot\artifacts\rc1\checksums",
  "$BuildRoot\artifacts\rc1\manifests",
  "$BuildRoot\logs\builds",
  "$BuildRoot\tools"
)
foreach ($d in $dirs) {
  if (-not (Test-Path $d)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    Write-Host "CREATED $d"
  } else {
    Write-Host "EXISTS $d"
  }
}
