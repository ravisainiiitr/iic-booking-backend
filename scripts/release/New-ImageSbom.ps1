#Requires -Version 5.1
<#
.SYNOPSIS
  Generate SBOM for a Docker image when syft/trivy is available; otherwise skip.
#>
param(
    [Parameter(Mandatory = $true)][string]$Image,
    [Parameter(Mandatory = $true)][string]$OutFile
)

$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $OutFile
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

$syft = Get-Command syft -ErrorAction SilentlyContinue
$trivy = Get-Command trivy -ErrorAction SilentlyContinue

if ($syft) {
    & syft $Image -o cyclonedx-json="$OutFile"
    if ($LASTEXITCODE -ne 0) { throw "syft failed: $LASTEXITCODE" }
    Write-Host "SBOM(syft)=$OutFile"
    exit 0
}

if ($trivy) {
    & trivy image --format cyclonedx --output $OutFile $Image
    if ($LASTEXITCODE -ne 0) { throw "trivy failed: $LASTEXITCODE" }
    Write-Host "SBOM(trivy)=$OutFile"
    exit 0
}

$skip = @{ status = 'skipped'; reason = 'syft/trivy not installed'; image = $Image } | ConvertTo-Json
Set-Content -Path ($OutFile -replace '\.json$', '.skipped.json') -Value $skip -Encoding utf8
Write-Warning 'SBOM skipped - install syft or trivy on the build host for Phase 2 enforcement.'
