<#
Local acceptance environment for the external-booking enhancement (backend worktree only).

  .\scripts\extbook_local\extbook-local.ps1 check                 # verify target (no changes)
  .\scripts\extbook_local\extbook-local.ps1 create-db             # create and mark iic_extbook_accept_dev if missing
  .\scripts\extbook_local\extbook-local.ps1 migrate
  .\scripts\extbook_local\extbook-local.ps1 seed                  # synthetic users, equipment, slots, flags
  .\scripts\extbook_local\extbook-local.ps1 snapshot | restore    # marked seeded copy / reset from it
  .\scripts\extbook_local\extbook-local.ps1 backend [-Clock 2026-10-28T21:05:00+05:30]
  .\scripts\extbook_local\extbook-local.ps1 task <name> [-Clock ...] [-MailFiles]   # run one scheduled task once
  .\scripts\extbook_local\extbook-local.ps1 pay <booking_id> [-Outcome success|failure] [-Clock ...]  # mock gateway
  .\scripts\extbook_local\extbook-local.ps1 backend -MockGateway   # server with the mock gateway (browser checkout)
  .\scripts\extbook_local\extbook-local.ps1 pay-checkout <checkout id> [-Outcome success|failure]  # via webhook
  .\scripts\extbook_local\extbook-local.ps1 courier catalog                      # synthetic courier list
  .\scripts\extbook_local\extbook-local.ps1 courier <booking_id> -Outcome in_transit|out_for_delivery|delivered|...
  .\scripts\extbook_local\extbook-local.ps1 payments | acceptance  # scripted payment scenarios / API end-to-end run

Every command uses config.settings.acceptance_local (see config/settings/acceptance_guards.py): explicit opt-in,
only iic_extbook_accept_dev on this machine carrying the disposable marker, never reads .env, console e-mail
(-MailFiles: .acceptance_local/mail), outbound network blocked. Only the two marked databases are ever dropped.
The database role is passwordless locally (trust or the user's pgpass); no credential is passed or printed.
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("check", "create-db", "migrate", "seed", "snapshot", "restore", "acceptance", "payments", "race", "backend", "task", "pay", "pay-checkout", "courier")]
    [string]$Command,
    [Parameter(Position = 1)]
    [string]$Target = "",
    [string]$Clock = "",
    [string]$DbUser = "iic_istem_test",
    [int]$Port = 8000,
    [string]$QrPython = "",
    [string]$QrScript = "",
    [string]$Outcome = "success",
    [switch]$MailFiles,
    [switch]$MockGateway
)

$ErrorActionPreference = "Stop"
$DbName = "iic_extbook_accept_dev"
$SnapshotName = "iic_extbook_accept_dev_seeded"  # only these two names are ever created or dropped
$DbHost = "127.0.0.1"
$DbPort = 5432

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if ((Split-Path -Leaf $Root) -ne "wt-be-extbook") {
    throw "Refusing to run outside the wt-be-extbook worktree (found $Root)."
}
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Missing $Python (create the worktree venv first)." }
if ($DbUser -notmatch '^[A-Za-z0-9_]+$') { throw "Invalid database role name." }

$Marker = "iic-extbook-local-acceptance:disposable"  # COMMENT ON DATABASE; set only by create-db / snapshot

$env:IIC_ACCEPTANCE_ENV = "local-acceptance"
$env:DJANGO_SETTINGS_MODULE = "config.settings.acceptance_local"
$env:DJANGO_READ_DOT_ENV_FILE = "False"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:DATABASE_URL = "postgresql://${DbUser}@${DbHost}:${DbPort}/${DbName}"
if ($Clock) { $env:ACCEPTANCE_CLOCK = $Clock } else { Remove-Item Env:ACCEPTANCE_CLOCK -ErrorAction SilentlyContinue }
if ($MailFiles -or $Command -eq "acceptance") { $env:ACCEPTANCE_EMAIL_FILES = "1" } else { Remove-Item Env:ACCEPTANCE_EMAIL_FILES -ErrorAction SilentlyContinue }

function Invoke-Psql([string]$Database, [string]$Sql) {
    $out = & psql -h $DbHost -p $DbPort -U $DbUser -d $Database -w -At -c $Sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed." }
    return $out
}

function Assert-LocalServer {
    $addr = Invoke-Psql "postgres" "select coalesce(host(inet_server_addr()),'socket') || '|' || current_setting('port')"
    if ($addr -notin @("127.0.0.1|$DbPort", "::1|$DbPort")) { throw "PostgreSQL server is not local ($addr)." }
    Write-Host "Target: host $DbHost, port $DbPort, database $DbName (server reports $addr)"
}

function Test-DbExists([string]$Name) {
    return (Invoke-Psql "postgres" "select 1 from pg_database where datname = '$Name'") -eq "1"
}

function Assert-Disposable([string]$Name) {
    # Positive identification before anything is dropped or copied over: the marker only this script sets.
    $comment = Invoke-Psql "postgres" "select coalesce(shobj_description(oid, 'pg_database'), '') from pg_database where datname = '$Name'"
    if ($comment -ne $Marker) { throw "Database $Name is not marked as the disposable acceptance database; refusing." }
}

function Set-DisposableMarker([string]$Name) {
    Invoke-Psql "postgres" "COMMENT ON DATABASE $Name IS '$Marker'" | Out-Null
}

function Invoke-Guard {
    Assert-LocalServer
    & $Python (Join-Path $PSScriptRoot "guard.py")
    if ($LASTEXITCODE -ne 0) { throw "Guard check failed; nothing was changed." }
}

Push-Location $Root
try {
    switch ($Command) {
        "check" { Invoke-Guard }
        "create-db" {
            Assert-LocalServer
            if (Test-DbExists $DbName) {
                # Never adopt an existing database: it must already carry the marker from an earlier create-db.
                Assert-Disposable $DbName
                Write-Host "Database $DbName already exists and is marked disposable."
            }
            else {
                & createdb -h $DbHost -p $DbPort -U $DbUser -w $DbName
                if ($LASTEXITCODE -ne 0) { throw "createdb failed." }
                Set-DisposableMarker $DbName
                Write-Host "Created $DbName on ${DbHost}:${DbPort} and marked it disposable."
            }
        }
        "migrate" { Invoke-Guard; & $Python manage.py migrate --noinput; exit $LASTEXITCODE }
        "seed" { Invoke-Guard; & $Python (Join-Path $PSScriptRoot "seed_acceptance.py"); exit $LASTEXITCODE }
        "snapshot" {
            # Copy of the freshly seeded database, used by "restore" to rerun the acceptance flow from scratch.
            Assert-LocalServer
            Assert-Disposable $DbName
            if (Test-DbExists $SnapshotName) {
                Assert-Disposable $SnapshotName
                & dropdb -h $DbHost -p $DbPort -U $DbUser -w $SnapshotName; if ($LASTEXITCODE -ne 0) { throw "dropdb failed." }
            }
            & createdb -h $DbHost -p $DbPort -U $DbUser -w -T $DbName $SnapshotName
            if ($LASTEXITCODE -ne 0) { throw "createdb from template failed (stop the backend first)." }
            Set-DisposableMarker $SnapshotName  # comments are not copied by CREATE DATABASE ... TEMPLATE
            Write-Host "Snapshot $SnapshotName created from $DbName."
        }
        "restore" {
            Assert-LocalServer
            if (-not (Test-DbExists $SnapshotName)) { throw "No snapshot $SnapshotName; run seed then snapshot first." }
            Assert-Disposable $SnapshotName
            if (Test-DbExists $DbName) {
                Assert-Disposable $DbName
                & dropdb -h $DbHost -p $DbPort -U $DbUser -w $DbName
                if ($LASTEXITCODE -ne 0) { throw "dropdb failed (stop the backend first)." }
            }
            & createdb -h $DbHost -p $DbPort -U $DbUser -w -T $SnapshotName $DbName
            if ($LASTEXITCODE -ne 0) { throw "createdb from snapshot failed." }
            Set-DisposableMarker $DbName
            Write-Host "$DbName restored from $SnapshotName."
        }
        "payments" {
            Invoke-Guard; & $Python (Join-Path $PSScriptRoot "payment_scenarios.py"); exit $LASTEXITCODE
        }
        "race" {
            $rounds = if ($Target) { $Target } else { "20" }
            if ($rounds -notmatch '^\d+$') { throw "Give the number of rounds per timing." }
            Invoke-Guard; & $Python (Join-Path $PSScriptRoot "race_scenarios.py") $rounds; exit $LASTEXITCODE
        }
        "acceptance" {
            Invoke-Guard
            $extra = @()
            if ($QrPython -and $QrScript) { $extra = @($QrPython, $QrScript) }
            & $Python (Join-Path $PSScriptRoot "acceptance_api.py") @extra
            exit $LASTEXITCODE
        }
        "backend" {
            Invoke-Guard
            Write-Host "Backend: http://127.0.0.1:$Port/api/  (Ctrl+C to stop)"
            if ($MockGateway) {
                & $Python (Join-Path $PSScriptRoot "serve_mock_gateway.py") "$Port"
            } else {
                & $Python manage.py runserver "127.0.0.1:$Port" --noreload
            }
            exit $LASTEXITCODE
        }
        "task" {
            if (-not $Target) { throw "Name the task: prepare_window, expire_special_allocations, queue_completed_invoice_requests, sric_invoice_batch, notifications_sweep." }
            Invoke-Guard; & $Python (Join-Path $PSScriptRoot "run_task.py") $Target; exit $LASTEXITCODE
        }
        "pay" {
            if ($Target -notmatch '^\d+$') { throw "Give the numeric booking id." }
            if ($Outcome -notin @("success", "failure")) { throw "Outcome must be success or failure." }
            Invoke-Guard; & $Python (Join-Path $PSScriptRoot "mock_razorpay.py") $Target $Outcome; exit $LASTEXITCODE
        }
        "courier" {
            if ($Target -eq "catalog") {
                Invoke-Guard; & $Python (Join-Path $PSScriptRoot "mock_courier.py") catalog; exit $LASTEXITCODE
            }
            if ($Target -notmatch '^\d+$') { throw "Give 'catalog' or the numeric booking id." }
            $states = @("info_received", "in_transit", "out_for_delivery", "delivered", "attempt_fail", "exception", "returned")
            if ($Outcome -notin $states) { throw "Outcome must be one of: $($states -join ', ')." }
            Invoke-Guard; & $Python (Join-Path $PSScriptRoot "mock_courier.py") $Target $Outcome; exit $LASTEXITCODE
        }
        "pay-checkout" {
            if ($Target -notmatch '^[0-9a-fA-F-]{36}$') { throw "Give the checkout id (UUID shown in the checkout URL)." }
            if ($Outcome -notin @("success", "failure")) { throw "Outcome must be success or failure." }
            Invoke-Guard; & $Python (Join-Path $PSScriptRoot "mock_razorpay.py") $Target $Outcome; exit $LASTEXITCODE
        }
    }
}
finally {
    Pop-Location
}
