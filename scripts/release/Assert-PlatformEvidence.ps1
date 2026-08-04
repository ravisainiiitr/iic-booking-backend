#Requires -Version 5.1
<#
.SYNOPSIS
  Fail closed unless required platform release evidence files exist.
#>
param(
    [Parameter(Mandatory = $true)][string]$EvidenceRoot,
    [Parameter(Mandatory = $true)][string]$PlatformVersion,
    [switch]$RequireSbom,
    [switch]$RequireFreezeCertificate,
    [switch]$RequirePublicationReport
)

$ErrorActionPreference = 'Stop'
$failed = New-Object System.Collections.Generic.List[string]

function Require-File([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) {
        $script:failed.Add("MISSING $Label : $Path")
    } else {
        Write-Host "PASS $Label"
    }
}

$be = Join-Path $EvidenceRoot 'backend'
$fe = Join-Path $EvidenceRoot 'frontend'
$dsa = Join-Path $EvidenceRoot 'dsa'
$raa = Join-Path $EvidenceRoot 'raa'

Require-File (Join-Path $be 'backend-images.json') 'Backend Docker Image Digest manifest'
Require-File (Join-Path $be 'docker-image-ids.txt') 'Backend Docker image ID ledger'
Require-File (Join-Path $fe 'frontend-image.json') 'Frontend Docker Image Digest manifest'
Require-File (Join-Path $dsa 'ArtifactChecksums-SHA256.txt') 'DSA Installer SHA256'
Require-File (Join-Path $raa 'ArtifactChecksums-SHA256.txt') 'RAA Installer SHA256'

if ($RequireSbom) {
    $sbom = @(
        (Join-Path $be 'sbom-django.cdx.json'),
        (Join-Path $be 'sbom-django.cdx.skipped.json')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $sbom) { $failed.Add('MISSING SBOM (or skipped marker) for Backend django image') }
    else { Write-Host "PASS SBOM evidence ($sbom)" }
}

$repoDocs = Join-Path $PSScriptRoot '..\..\docs\release'
if ($RequireFreezeCertificate) {
    Require-File (Join-Path $repoDocs 'Platform-RC1-Freeze-Certificate.md') 'Freeze Certificate'
}
if ($RequirePublicationReport) {
    # Publication report / ledger naming
    $ledger = @(
        (Join-Path $repoDocs 'Platform-RC1-Publication-Report.md'),
        (Join-Path $EvidenceRoot 'Release-Ledger.md')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $ledger) { $failed.Add('MISSING Release Ledger / Publication Report') }
    else { Write-Host "PASS Release Ledger ($ledger)" }
}

# Digests must be non-empty JSON arrays/objects with id fields
try {
    $imgs = Get-Content (Join-Path $be 'backend-images.json') -Raw | ConvertFrom-Json
    if (-not $imgs -or (@($imgs).Count -lt 1)) { $failed.Add('Backend image digest file empty') }
    else {
        foreach ($i in @($imgs)) {
            if (-not $i.id) { $failed.Add("Backend image entry missing id: $($i.image)") }
        }
    }
} catch { $failed.Add("Backend images JSON invalid: $_") }

try {
    $f = Get-Content (Join-Path $fe 'frontend-image.json') -Raw | ConvertFrom-Json
    if (-not $f.id) { $failed.Add('Frontend image missing id') }
} catch { $failed.Add("Frontend image JSON invalid: $_") }

foreach ($p in @(
    (Join-Path $dsa 'ArtifactChecksums-SHA256.txt'),
    (Join-Path $raa 'ArtifactChecksums-SHA256.txt')
)) {
    if (Test-Path $p) {
        $lines = @(Get-Content $p | Where-Object { $_.Trim() -ne '' })
        if ($lines.Count -lt 1) { $failed.Add("Empty checksum file: $p") }
        elseif ($lines[0] -notmatch '^[A-Fa-f0-9]{64}\s+') { $failed.Add("Checksum file not SHA256 format: $p") }
    }
}

if ($failed.Count -gt 0) {
    Write-Host "PLATFORM_EVIDENCE_FAILED version=$PlatformVersion"
    $failed | ForEach-Object { Write-Host " - $_" }
    Write-Host 'Never generating GREEN platform release without evidence.'
    exit 1
}

Write-Host "PLATFORM_EVIDENCE_OK version=$PlatformVersion"
exit 0
