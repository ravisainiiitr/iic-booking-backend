<#
.SYNOPSIS
  Portal-side deployment verification (Portal, DB signals via API, storage, workspace, sync, Guacamole, config).
  Safe to re-run. Prefer running on an admin workstation with network access to Portal.
.PARAMETER BaseUrl
  Portal origin
.PARAMETER Token
  Bearer token for a CanManageRemoteAnalysis user
#>
param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$Token
)

$ErrorActionPreference = "Continue"
$BaseUrl = $BaseUrl.TrimEnd("/")
$headers = @{ Authorization = "Bearer $Token" }
$pass = 0
$fail = 0

function Write-Check([string]$Name, [bool]$Ok, [string]$Detail = "") {
    if ($Ok) {
        Write-Host ("PASS  {0} {1}" -f $Name, $Detail) -ForegroundColor Green
        $script:pass++
    } else {
        Write-Host ("FAIL  {0} {1}" -f $Name, $Detail) -ForegroundColor Red
        $script:fail++
    }
}

Write-Host "=== VerifyPortal ===" -ForegroundColor Cyan

# Health / Guacamole / config via readiness + diagnostics
try {
    $ready = Invoke-RestMethod -Uri "$BaseUrl/api/v1/analysis/health/ready/" -TimeoutSec 30
    Write-Check "Portal readiness" ($ready.status -eq "ready") ($ready | ConvertTo-Json -Compress)
    Write-Check "Guacamole ok" ($ready.checks.guacamole -eq "ok") ("guacamole=" + $ready.checks.guacamole)
    Write-Check "Enrollment configured" ($ready.checks.agent_enrollment -eq "configured") ("enrollment=" + $ready.checks.agent_enrollment)
    Write-Check "Database ok" ($ready.checks.database -eq "ok")
} catch {
    Write-Check "Portal readiness" $false $_.Exception.Message
}

$diag = $null
try {
    $diag = Invoke-RestMethod -Uri "$BaseUrl/api/v1/analysis/operations/diagnostics/" -Headers $headers -TimeoutSec 60
    Write-Check "Diagnostics" ($null -ne $diag)
} catch {
    Write-Check "Diagnostics" $false $_.Exception.Message
}

if ($diag) {
    Write-Check "DEBUG off" (-not [bool]$diag.django.DEBUG)
    Write-Check "mock_guacamole off" (-not [bool]$diag.settings.mock_guacamole)
    Write-Check "Storage workspace writable" ([bool]$diag.storage.workspace_root_writable) ($diag.storage.workspace_root)
    Write-Check "Storage archive writable" ([bool]$diag.storage.archive_root_writable) ($diag.storage.archive_root)
    Write-Check "Celery broker configured" ([bool]$diag.celery.broker_configured)
    $beat = @($diag.celery.beat_entries)
    Write-Check "Celery Beat RAA tasks registered" ($beat.Count -ge 1 -or $diag.celery.beat_table -eq "ok") ("entries=" + $beat.Count)
    Write-Check "Agent registered" (@($diag.workstations).Count -ge 1) ("count=" + @($diag.workstations).Count)
    Write-Check "No cleanup failure workspaces" ([int]$diag.workspaces.cleanup_failures -eq 0) ("cleanup_failures=" + $diag.workspaces.cleanup_failures)
    Write-Check "Workspace sync overview present" ($null -ne $diag.workspaces.by_sync_phase)
}

# Workspace / sync APIs
try {
    $ws = Invoke-RestMethod -Uri "$BaseUrl/api/v1/analysis/workspaces/dashboard/" -Headers $headers -TimeoutSec 30
    Write-Check "Workspace dashboard" ($null -ne $ws)
} catch {
    Write-Check "Workspace dashboard" $false $_.Exception.Message
}

try {
    $sess = Invoke-RestMethod -Uri "$BaseUrl/api/v1/analysis/session/dashboard/" -Headers $headers -TimeoutSec 30
    Write-Check "Session dashboard" ($null -ne $sess)
} catch {
    Write-Check "Session dashboard" $false $_.Exception.Message
}

try {
    $hb = Invoke-RestMethod -Uri "$BaseUrl/api/v1/analysis/heartbeats/" -Headers $headers -TimeoutSec 30
    Write-Check "Heartbeats API" ($null -ne $hb)
} catch {
    Write-Check "Heartbeats API" $false $_.Exception.Message
}

try {
    $sched = Invoke-RestMethod -Uri "$BaseUrl/api/v1/analysis/scheduler/status/" -Headers $headers -TimeoutSec 30
    Write-Check "Scheduler status" ($null -ne $sched)
} catch {
    Write-Check "Scheduler status" $false $_.Exception.Message
}

Write-Host ""
Write-Host "Open HTML diagnostics: $BaseUrl/api/v1/analysis/operations/diagnostics/?view=html"
Write-Host ("Summary: PASS={0} FAIL={1}" -f $pass, $fail)
if ($fail -gt 0) { exit 1 } else { exit 0 }
