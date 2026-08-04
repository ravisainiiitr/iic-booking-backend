#Requires -Version 5.1
<#
.SYNOPSIS
  Build a Deployment Center metadata package (JSON) for installer publication.
  Does not upload unless -Upload is set (and DRY_RUN is not true).
#>
param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$Sha256,
    [Parameter(Mandatory = $true)][string]$FilePath,
    [string]$MinPlatformVersion = '2.5.0-rc1',
    [string]$MaxPlatformVersion = '',
    [ValidateSet('RC', 'GA', 'Deprecated', 'Withdrawn')][string]$Status = 'RC',
    [string]$OutFile = '',
    [string]$DcBaseUrl = '',
    [string]$DcToken = '',
    [switch]$Upload,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$pkg = [ordered]@{
    name                 = $Name
    version              = $Version
    sha256               = $Sha256
    file                 = $FilePath
    min_platform_version = $MinPlatformVersion
    max_platform_version = $MaxPlatformVersion
    status               = $Status
    generated_at         = (Get-Date).ToUniversalTime().ToString('o')
}

if (-not $OutFile) {
    $OutFile = Join-Path (Split-Path $FilePath -Parent) ("dc-metadata-{0}-{1}.json" -f $Name, $Version)
}
($pkg | ConvertTo-Json -Depth 5) | Set-Content -Path $OutFile -Encoding utf8
Write-Host "DC_METADATA=$OutFile"

if (-not $Upload) { return }

if ($DryRun -or $env:DRY_RUN -eq 'true') {
    Write-Warning "DRY_RUN: skipping Deployment Center upload"
    return
}

if (-not $DcBaseUrl -or -not $DcToken) {
    throw "DcBaseUrl and DcToken required for upload"
}

# Placeholder endpoint — align with portal Deployment Center API when wiring Batch 7.
$uri = ($DcBaseUrl.TrimEnd('/') + '/api/v1/deployment/installer-releases/')
$headers = @{ Authorization = "Bearer $DcToken" }
Write-Host "UPLOAD $uri"
# Multipart upload left as invoke pattern for operators to refine against live API schema.
Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -ContentType 'application/json' -Body ($pkg | ConvertTo-Json)
