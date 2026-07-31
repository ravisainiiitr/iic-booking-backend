#!/usr/bin/env pwsh
<#
.SYNOPSIS
  One-command Analysis Platform regression harness.

.DESCRIPTION
  Seeds APT lab data, runs API/security/mock-agent tests, optional perf/lab/e2e,
  writes HTML/JSON/JUnit reports under tests/analysis_platform/report/.

  Does NOT modify production business logic.
#>
param(
  [switch]$Perf,
  [switch]$Lab,
  [switch]$E2E,
  [switch]$SkipCleanup,
  [string]$AgentId = $env:ANALYSIS_AGENT_ID
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "manage.py"))) {
  # scripts/ is under backend root
  $Root = Resolve-Path (Join-Path $PSScriptRoot "..")
}
Set-Location $Root

$ReportDir = Join-Path $Root "tests\analysis_platform\report"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
$JUnit = Join-Path $ReportDir "pytest-junit.xml"
$Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

Write-Host "=== Analysis Platform Test Harness ===" -ForegroundColor Cyan
Write-Host "Root: $Root"
Write-Host "Started: $Stamp"

$env:DJANGO_SETTINGS_MODULE = if ($env:DJANGO_SETTINGS_MODULE) { $env:DJANGO_SETTINGS_MODULE } else { "config.settings.test" }

$markers = "analysis_platform"
if ($Perf) {
  $env:ANALYSIS_PERF = "1"
  $markers = "$markers or analysis_perf"
}
if ($Lab) {
  $env:ANALYSIS_LAB = "1"
  if ($AgentId) { $env:ANALYSIS_AGENT_ID = $AgentId }
  $markers = "$markers or analysis_lab"
}

$Python = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

Write-Host "`n[1/5] Running pytest harness..." -ForegroundColor Yellow
$pytestArgs = @(
  "-m", "pytest",
  "-m", $markers,
  "tests/analysis_platform",
  "--junitxml=$JUnit",
  "-q"
)
$pytestExit = 0
try {
  & $Python @pytestArgs
  $pytestExit = $LASTEXITCODE
} catch {
  $pytestExit = 1
  Write-Warning $_
}

Write-Host "`n[2/5] Building dashboard from JUnit..." -ForegroundColor Yellow
& $Python -c @"
from pathlib import Path
from tests.analysis_platform.reporting import write_dashboard
out = write_dashboard(
    report_dir=Path(r'$ReportDir'),
    junit_path=Path(r'$JUnit'),
    metrics={'runner': 'run-analysis-tests.ps1'},
)
print('DASHBOARD', out['html'])
print('PASSED', out['summary']['passed'], 'FAILED', out['summary']['failed'], 'SKIPPED', out['summary']['skipped'])
"@

if ($E2E) {
  Write-Host "`n[3/5] Playwright E2E..." -ForegroundColor Yellow
  $env:ANALYSIS_E2E = "1"
  $E2EDir = Join-Path $Root "tests\analysis_platform\e2e"
  Push-Location $E2EDir
  if (-not (Test-Path "node_modules")) {
    npm install
    npx playwright install chromium
  }
  npx playwright test
  $e2eExit = $LASTEXITCODE
  Pop-Location
  if ($e2eExit -ne 0) { $pytestExit = $e2eExit }
} else {
  Write-Host "`n[3/5] Playwright skipped (pass -E2E)" -ForegroundColor DarkGray
}

if (-not $SkipCleanup) {
  Write-Host "`n[4/5] Cleanup note: Django test DB rolls back; scripted APT rows cleaned only if ANALYSIS_CLEANUP=1" -ForegroundColor Yellow
  if ($env:ANALYSIS_CLEANUP -eq "1") {
    & $Python -c "import django; django.setup(); from tests.analysis_platform.utils.cleanup import cleanup_apt_prefix; print('cleaned', cleanup_apt_prefix())"
  }
} else {
  Write-Host "`n[4/5] Cleanup skipped" -ForegroundColor DarkGray
}

Write-Host "`n[5/5] Summary" -ForegroundColor Green
Write-Host "JUnit: $JUnit"
Write-Host "HTML:  $(Join-Path $ReportDir 'dashboard.html')"
Write-Host "Exit:  $pytestExit"
exit $pytestExit
