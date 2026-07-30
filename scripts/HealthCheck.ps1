<#
.SYNOPSIS
  Remote Analysis portal health check (Windows). Safe to re-run.
.PARAMETER BaseUrl
  Portal origin, e.g. https://booking.example.edu
.PARAMETER Token
  Optional Bearer token (manage user) for authenticated checks / diagnostics.
.PARAMETER SkipAuth
  Skip manage-only endpoints.
#>
param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [string]$Token = "",
    [switch]$SkipAuth
)

$ErrorActionPreference = "Continue"
$BaseUrl = $BaseUrl.TrimEnd("/")
$fail = 0
$pass = 0

function Write-Check([string]$Name, [bool]$Ok, [string]$Detail = "") {
    if ($Ok) {
        Write-Host ("PASS  {0} {1}" -f $Name, $Detail) -ForegroundColor Green
        $script:pass++
    } else {
        Write-Host ("FAIL  {0} {1}" -f $Name, $Detail) -ForegroundColor Red
        $script:fail++
    }
}

function Invoke-Json([string]$Url, [hashtable]$Headers = @{}) {
    try {
        return Invoke-RestMethod -Uri $Url -Headers $Headers -Method Get -TimeoutSec 30
    } catch {
        return $null
    }
}

Write-Host "=== Remote Analysis HealthCheck ===" -ForegroundColor Cyan
Write-Host "BaseUrl=$BaseUrl"

# Liveness
$live = Invoke-Json "$BaseUrl/api/v1/analysis/health/live/"
Write-Check "Database/API liveness" ($null -ne $live -and $live.status -eq "ok") ($live | ConvertTo-Json -Compress)

# Readiness
try {
    $readyResp = Invoke-WebRequest -Uri "$BaseUrl/api/v1/analysis/health/ready/" -Method Get -TimeoutSec 30 -UseBasicParsing
    $ready = $readyResp.Content | ConvertFrom-Json
    $readyOk = $readyResp.StatusCode -eq 200 -and $ready.status -eq "ready"
    Write-Check "Readiness (DB/cache/Guacamole/enrollment)" $readyOk ($ready | ConvertTo-Json -Compress)
} catch {
    $body = $_.ErrorDetails.Message
    Write-Check "Readiness (DB/cache/Guacamole/enrollment)" $false $body
}

# Combined health
$health = Invoke-Json "$BaseUrl/api/v1/analysis/health/"
Write-Check "Combined health" ($null -ne $health) ("status=" + $(if ($health) { $health.status } else { "n/a" }))

$headers = @{}
if ($Token) { $headers["Authorization"] = "Bearer $Token" }

if (-not $SkipAuth -and $Token) {
    $dash = Invoke-Json "$BaseUrl/api/v1/analysis/operations/dashboard/" $headers
    Write-Check "Operations dashboard" ($null -ne $dash)

    $diag = Invoke-Json "$BaseUrl/api/v1/analysis/operations/diagnostics/" $headers
    Write-Check "Diagnostics API" ($null -ne $diag)
    if ($diag) {
        Write-Check "Storage writable (diag)" ([bool]$diag.storage.workspace_root_writable) ($diag.storage | ConvertTo-Json -Compress)
        Write-Check "Celery broker configured (diag)" ([bool]$diag.celery.broker_configured)
        $wsCount = @($diag.workstations).Count
        Write-Check "Workstations registered" ($wsCount -ge 0) "count=$wsCount"
        $stale = @($diag.workstations | Where-Object { $_.heartbeat_age_seconds -ne $null -and $_.heartbeat_age_seconds -gt 120 }).Count
        Write-Check "Heartbeat freshness (<120s stale count=0 preferred)" ($stale -eq 0) "stale=$stale"
        $mock = [bool]$diag.settings.mock_guacamole
        Write-Check "mock_guacamole disabled" (-not $mock) "mock=$mock"
        $debug = [bool]$diag.django.DEBUG
        Write-Check "DEBUG disabled" (-not $debug) "DEBUG=$debug"
    }

    $sched = Invoke-Json "$BaseUrl/api/v1/analysis/scheduler/status/" $headers
    Write-Check "Scheduler status API" ($null -ne $sched)

    $wsDash = Invoke-Json "$BaseUrl/api/v1/analysis/workspaces/dashboard/" $headers
    Write-Check "Workspace dashboard" ($null -ne $wsDash)
} elseif (-not $SkipAuth) {
    Write-Host "SKIP  Authenticated checks (pass -Token <jwt-or-drf-token>)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host ("Summary: PASS={0} FAIL={1}" -f $pass, $fail)
if ($fail -gt 0) { exit 1 } else { exit 0 }
