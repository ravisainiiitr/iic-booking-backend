#Requires -Version 5.1
<#
.SYNOPSIS
  Record Docker image digests/IDs for release manifests.
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Images,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
$items = @()
foreach ($img in $Images) {
    $id = docker image inspect $img --format '{{.Id}}' 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $id) {
        throw "Image not found: $img"
    }
    $digest = docker image inspect $img --format '{{index .RepoDigests 0}}' 2>$null
    $created = docker image inspect $img --format '{{.Created}}' 2>$null
    $items += [ordered]@{
        image   = $img
        version = $Version
        id      = $id.Trim()
        digest  = if ($digest) { $digest.Trim() } else { '' }
        created = if ($created) { $created.Trim() } else { '' }
    }
    Write-Host ("RECORDED {0} id={1} digest={2}" -f $img, $id.Trim(), $(if ($digest) { $digest.Trim() } else { '(local-only)' }))
}

$dir = Split-Path -Parent $OutFile
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
($items | ConvertTo-Json -Depth 5) | Set-Content -Path $OutFile -Encoding utf8
Write-Host "Wrote $OutFile"
