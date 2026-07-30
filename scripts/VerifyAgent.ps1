<#
.SYNOPSIS
  Verify Remote Analysis Agent on a Windows analysis workstation. Safe to re-run.
.PARAMETER ServiceName
  Windows service name (default RemoteAnalysisAgent)
.PARAMETER InstallDir
  Published agent directory (default C:\Services\RemoteAnalysisAgent)
.PARAMETER PortalBaseUrl
  Optional expected PortalBaseUrl to compare against appsettings.json
#>
param(
    [string]$ServiceName = "RemoteAnalysisAgent",
    [string]$InstallDir = "C:\Services\RemoteAnalysisAgent",
    [string]$PortalBaseUrl = "",
    [int]$LocalHealthPort = 5088
)

$ErrorActionPreference = "Continue"
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

Write-Host "=== VerifyAgent ===" -ForegroundColor Cyan

# Service
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
Write-Check "Service installed" ($null -ne $svc) $(if ($svc) { $svc.Status } else { "missing" })
Write-Check "Service running" ($null -ne $svc -and $svc.Status -eq "Running")

# Config
$cfgPath = Join-Path $InstallDir "appsettings.json"
Write-Check "appsettings.json exists" (Test-Path $cfgPath) $cfgPath
$portalOk = $false
$enrollmentSet = $false
if (Test-Path $cfgPath) {
    try {
        $json = Get-Content $cfgPath -Raw | ConvertFrom-Json
        $section = $json.RemoteAnalysisAgent
        $portal = [string]$section.PortalBaseUrl
        $enrollmentSet = -not [string]::IsNullOrWhiteSpace([string]$section.EnrollmentKey)
        Write-Check "PortalBaseUrl present" (-not [string]::IsNullOrWhiteSpace($portal)) $portal
        if ($PortalBaseUrl) {
            Write-Check "PortalBaseUrl matches expected" ($portal.TrimEnd("/") -eq $PortalBaseUrl.TrimEnd("/"))
        }
        Write-Check "EnrollmentKey configured" $enrollmentSet
        $portalOk = -not [string]::IsNullOrWhiteSpace($portal)
        $script:resolvedPortal = $portal
        $sessionRoot = [string]$section.SessionWorkspaceRoot
        if ([string]::IsNullOrWhiteSpace($sessionRoot)) {
            $sessionRoot = "$env:ProgramData\RemoteAnalysisAgent\Sessions"
        }
        $logDir = [string]$section.LogDirectory
        if ([string]::IsNullOrWhiteSpace($logDir)) {
            $logDir = "$env:ProgramData\RemoteAnalysisAgent\Logs"
        }
        $stateDir = [string]$section.StateDirectory
        if ([string]::IsNullOrWhiteSpace($stateDir)) {
            $stateDir = "$env:ProgramData\RemoteAnalysisAgent\State"
        }
        foreach ($d in @($sessionRoot, $logDir, $stateDir)) {
            if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
            Write-Check "Folder exists $d" (Test-Path $d)
            try {
                $probe = Join-Path $d (".ra_write_probe_" + [guid]::NewGuid().ToString("N"))
                Set-Content -Path $probe -Value "ok" -ErrorAction Stop
                Remove-Item $probe -Force -ErrorAction SilentlyContinue
                Write-Check "Folder writable $d" $true
            } catch {
                Write-Check "Folder writable $d" $false $_.Exception.Message
            }
        }
    } catch {
        Write-Check "Parse appsettings.json" $false $_.Exception.Message
    }
}

# Portal reachability
if ($portalOk -and $script:resolvedPortal) {
    $origin = $script:resolvedPortal.TrimEnd("/")
    try {
        $r = Invoke-WebRequest -Uri "$origin/api/v1/analysis/health/live/" -UseBasicParsing -TimeoutSec 20
        Write-Check "Portal reachable (health/live)" ($r.StatusCode -eq 200)
    } catch {
        Write-Check "Portal reachable (health/live)" $false $_.Exception.Message
    }
    try {
        # Heartbeat requires auth; unauthenticated should be 401/403 — proves route exists
        $hb = Invoke-WebRequest -Uri "$origin/api/v1/analysis/heartbeat/" -Method Post -UseBasicParsing -TimeoutSec 20 -ErrorAction SilentlyContinue
        Write-Check "Heartbeat endpoint reachable" $false "unexpected success without auth"
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        Write-Check "Heartbeat endpoint reachable" ($code -in 401, 403, 405, 400) "http=$code"
    }
}

# Local agent health
try {
    $lh = Invoke-RestMethod -Uri "http://127.0.0.1:$LocalHealthPort/api/health" -TimeoutSec 5
    Write-Check "Local agent health port" $true ($lh | ConvertTo-Json -Compress)
} catch {
    Write-Check "Local agent health port" $false "port=$LocalHealthPort (may be disabled if LocalHealthPort=0)"
}

# Inventory / logs evidence
$logRoot = "$env:ProgramData\RemoteAnalysisAgent\Logs"
if (Test-Path $logRoot) {
    $logs = Get-ChildItem $logRoot -Filter "raa-*.log" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Write-Check "Log file present" ($null -ne $logs) $(if ($logs) { $logs.FullName } else { "" })
}

Write-Host ""
Write-Host ("Summary: PASS={0} FAIL={1}" -f $pass, $fail)
Write-Host "Note: Software inventory is generated by the agent on its inventory interval; confirm Portal /api/v1/analysis/software/ after registration."
if ($fail -gt 0) { exit 1 } else { exit 0 }
