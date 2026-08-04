#Requires -Version 5.1
<#
.SYNOPSIS
  Generate SHA256 checksum file for release artifacts.
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Paths,
    [Parameter(Mandatory = $true)][string]$OutFile
)

$ErrorActionPreference = 'Stop'
$lines = New-Object System.Collections.Generic.List[string]
foreach ($p in $Paths) {
    if (-not (Test-Path -LiteralPath $p)) { throw "Missing artifact: $p" }
    $hash = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash
    $lines.Add(("{0}  {1}" -f $hash, (Resolve-Path -LiteralPath $p).Path))
    Write-Host ("SHA256 {0} = {1}" -f $p, $hash)
}
$dir = Split-Path -Parent $OutFile
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$lines | Set-Content -Path $OutFile -Encoding ascii
Write-Host "Wrote $OutFile"
