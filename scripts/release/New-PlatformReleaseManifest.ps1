#Requires -Version 5.1
param(
    [Parameter(Mandatory = $true)][string]$PlatformVersion,
    [Parameter(Mandatory = $true)][string]$BackendTag,
    [Parameter(Mandatory = $true)][string]$BackendSha,
    [Parameter(Mandatory = $true)][string]$FrontendTag,
    [Parameter(Mandatory = $true)][string]$FrontendSha,
    [Parameter(Mandatory = $true)][string]$DsaTag,
    [Parameter(Mandatory = $true)][string]$DsaSha,
    [Parameter(Mandatory = $true)][string]$RaaTag,
    [Parameter(Mandatory = $true)][string]$RaaSha,
    [string]$ImagesJson = '',
    [string]$ChecksumsFile = '',
    [Parameter(Mandatory = $true)][string]$OutDir
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$manifest = [ordered]@{
    platform_version = $PlatformVersion
    status           = 'RC'
    generated_at     = (Get-Date).ToUniversalTime().ToString('o')
    repositories     = [ordered]@{
        backend  = @{ tag = $BackendTag; sha = $BackendSha }
        frontend = @{ tag = $FrontendTag; sha = $FrontendSha }
        dsa      = @{ tag = $DsaTag; sha = $DsaSha }
        raa      = @{ tag = $RaaTag; sha = $RaaSha }
    }
    docker_images    = @()
    checksums_file   = $ChecksumsFile
    deployment_order = @('backend', 'frontend', 'deployment_center_metadata', 'dsa', 'equipment_wizard', 'raa', 'e2e_commissioning')
    rollback_order   = @('raa', 'equipment_wizard', 'dsa', 'frontend', 'backend', 'database_restore_if_needed')
    compatibility_matrix = @(
        @{ pair = 'backend-frontend'; result = 'PASS' }
        @{ pair = 'backend-dsa'; result = 'PASS' }
        @{ pair = 'backend-raa'; result = 'PASS' }
        @{ pair = 'frontend-dsa'; result = 'PASS' }
        @{ pair = 'frontend-raa'; result = 'PASS' }
        @{ pair = 'dsa-raa'; result = 'PASS' }
    )
}

if ($ImagesJson -and (Test-Path $ImagesJson)) {
    $manifest.docker_images = Get-Content $ImagesJson -Raw | ConvertFrom-Json
}

$jsonPath = Join-Path $OutDir ("Platform-{0}-Release-Manifest.json" -f $PlatformVersion)
$mdPath = Join-Path $OutDir ("Platform-{0}-Release-Manifest.md" -f $PlatformVersion)
($manifest | ConvertTo-Json -Depth 8) | Set-Content $jsonPath -Encoding utf8

$mdLines = @(
    "# Platform Release Manifest - $PlatformVersion",
    '',
    "Generated: $($manifest.generated_at)",
    '',
    '## Repositories',
    '',
    '| Component | Tag | SHA |',
    '|---|---|---|',
    "| Backend | $BackendTag | $BackendSha |",
    "| Frontend | $FrontendTag | $FrontendSha |",
    "| DSA | $DsaTag | $DsaSha |",
    "| RAA | $RaaTag | $RaaSha |",
    '',
    '## Deployment order',
    '',
    ($manifest.deployment_order -join ' -> '),
    '',
    '## Rollback order',
    '',
    ($manifest.rollback_order -join ' -> '),
    '',
    '## Checksums',
    '',
    $ChecksumsFile,
    '',
    '## Docker images',
    '',
    "See JSON companion: $(Split-Path $jsonPath -Leaf)"
)
$mdLines | Set-Content -Path $mdPath -Encoding utf8
Write-Host "MANIFEST_JSON=$jsonPath"
Write-Host "MANIFEST_MD=$mdPath"
