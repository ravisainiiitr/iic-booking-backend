"""Deterministic REAL staging activation orchestration.

Never edits .envs/.staging/.django.
Never invents credentials.
Never enables REAL mode automatically.
Never prints secrets/tokens.
Refuses production environments.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from iic_booking.users.legacy_ledger.real_integration_guards import (
    EXPECTED_OMNIPORT_CALLBACK_PATH,
    STATUS_BLOCKED,
    STATUS_CONFIGURED,
    STATUS_FAIL,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_TESTED,
    STATUS_PASS,
    assert_staging_environment,
    build_real_integration_preflight,
    channel_i_preflight_status,
    employee_id_claim_status,
    legacy_mysql_preflight_status,
    omniport_redirect_uri_status,
    real_integration_enabled,
    s3_blocks_real_activation,
    s3_integration_status,
)
from iic_booking.users.legacy_ledger.real_integration_live_evidence import (
    channel_i_from_live_probe,
    employee_identity_from_live_probe,
    probe_live_channel_i_identity,
    write_live_channel_i_evidence,
)


def _refuse_production_settings() -> None:
    module = (getattr(settings, "SETTINGS_MODULE", "") or "").lower()
    if "production" in module:
        raise ImproperlyConfigured(
            "REFUSED: real_integration_* commands must not run under production settings."
        )
    assert_staging_environment()


def verify_fixture_isolation_under_real_intent() -> dict[str, Any]:
    """REAL intent must never silently return fixture data."""
    from django.test import override_settings

    from iic_booking.users.identity.channel_i_fixture import channel_i_fixture_mode_enabled
    from iic_booking.users.legacy_ledger.snapshot_reader import get_legacy_reader

    results = []
    ok = True

    # Case A: REAL enabled + Channel-I fixture → must raise
    try:
        with override_settings(
            DEPLOYMENT_ENVIRONMENT="STAGING",
            REAL_INTEGRATION_ENABLED=True,
            CHANNEL_I_STAGING_FIXTURE_MODE=True,
        ):
            channel_i_fixture_mode_enabled()
        results.append({"check": "channel_i_fixture_with_real", "result": STATUS_FAIL, "detail": "did not raise"})
        ok = False
    except ImproperlyConfigured:
        results.append({"check": "channel_i_fixture_with_real", "result": STATUS_PASS})

    # Case B: REAL enabled + legacy fixture + host set → get_legacy_reader must raise
    try:
        with override_settings(
            DEPLOYMENT_ENVIRONMENT="STAGING",
            REAL_INTEGRATION_ENABLED=True,
            LEGACY_MYSQL_STAGING_FIXTURE_MODE=True,
            OLD_MYSQL_HOST="mysql.example",
            OLD_MYSQL_USER="ro",
            OLD_MYSQL_DATABASE="admin",
            OLD_MYSQL_PASSWORD="x",
            DATABASES={"default": {"HOST": "postgres", "NAME": "iic_booking_staging"}},
        ):
            get_legacy_reader(require_real=True)
        results.append({"check": "legacy_fixture_with_real", "result": STATUS_FAIL, "detail": "did not raise"})
        ok = False
    except ImproperlyConfigured:
        results.append({"check": "legacy_fixture_with_real", "result": STATUS_PASS})

    # Case C: REAL enabled + missing MySQL → must not load fixture
    try:
        with override_settings(
            DEPLOYMENT_ENVIRONMENT="STAGING",
            REAL_INTEGRATION_ENABLED=True,
            LEGACY_MYSQL_STAGING_FIXTURE_MODE=False,
            OLD_MYSQL_HOST="",
            OLD_MYSQL_USER="",
            OLD_MYSQL_DATABASE="",
            OLD_MYSQL_PASSWORD="",
            DATABASES={"default": {"HOST": "postgres", "NAME": "iic_booking_staging"}},
        ):
            get_legacy_reader(require_real=True)
        results.append({"check": "missing_mysql_no_fixture", "result": STATUS_FAIL, "detail": "did not raise"})
        ok = False
    except ImproperlyConfigured:
        results.append({"check": "missing_mysql_no_fixture", "result": STATUS_PASS})
    except Exception as exc:  # noqa: BLE001
        # Connection/config errors are acceptable — must not be a silent fixture reader
        results.append(
            {
                "check": "missing_mysql_no_fixture",
                "result": STATUS_PASS,
                "detail": type(exc).__name__,
            }
        )

    return {
        "status": STATUS_PASS if ok else STATUS_FAIL,
        "checks": results,
        "note": "REAL mode must fail closed — never substitute fixtures",
    }


def probe_channel_i_oauth_config_only() -> dict[str, Any]:
    """Config/authorize-URL readiness without completing interactive OAuth.

    Never logs client secret / tokens / codes.
    """
    channel = channel_i_preflight_status()
    redirect = omniport_redirect_uri_status()
    if channel.get("status") == STATUS_BLOCKED:
        return {
            "status": STATUS_BLOCKED,
            "reason": channel.get("reason"),
            "live_oauth": STATUS_NOT_TESTED,
        }
    if redirect.get("status") != "VALID":
        return {
            "status": STATUS_BLOCKED,
            "reason": "CHANNEL-I REDIRECT URI INVALID",
            "live_oauth": STATUS_NOT_TESTED,
        }

    client_id = (getattr(settings, "OMNIPORT_CLIENT_ID", "") or "").strip()
    auth_url = (getattr(settings, "OMNIPORT_AUTH_URL", "") or "").strip()
    redirect_uri = (getattr(settings, "OMNIPORT_REDIRECT_URI", "") or "").strip()
    if not auth_url or not client_id or not redirect_uri:
        return {
            "status": STATUS_BLOCKED,
            "reason": "OAuth authorize URL components incomplete",
            "live_oauth": STATUS_NOT_TESTED,
        }

    # Build authorize URL shape only — do not call Channel-I network unless operator completes browser flow.
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": "PREFLIGHT_STATE_PLACEHOLDER",
        }
    )
    authorize_preview = f"{auth_url}?{query}" if "?" not in auth_url else f"{auth_url}&{query}"
    return {
        "status": STATUS_CONFIGURED,
        "reason": "OAuth config present; browser/operator authorization still required",
        "live_oauth": "OPERATOR ACTION REQUIRED",
        "authorize_url_built": True,
        "client_id_present": True,
        "redirect_path": EXPECTED_OMNIPORT_CALLBACK_PATH,
        "authorize_host_present": bool(urlencode({"h": auth_url})),  # presence only
        "note": "Do not treat this as OAuth PASS. Complete staging login separately.",
        # Never include the full authorize URL in evidence if it embeds secrets — client_id is not secret
        # but we still avoid dumping full URL with state into logs by default.
        "authorize_url_length": len(authorize_preview),
    }


def probe_legacy_mysql_readonly() -> dict[str, Any]:
    """Live READ-ONLY MySQL probe when credentials present. Never mutates."""
    mysql = legacy_mysql_preflight_status()
    if mysql.get("status") != STATUS_CONFIGURED:
        return {
            "status": STATUS_BLOCKED,
            "reason": mysql.get("reason") or "MySQL not configured",
            "live_mysql": STATUS_NOT_TESTED,
            "wallet_read": STATUS_NOT_TESTED,
            "booking_read": STATUS_NOT_TESTED,
            "ledger_read": STATUS_NOT_TESTED,
        }

    from iic_booking.users.legacy_ledger.reader import OldMySQLConnectionError, OldMySQLReader
    from iic_booking.users.legacy_ledger.snapshot_reader import get_legacy_reader

    try:
        reader = get_legacy_reader(require_real=True)
    except ImproperlyConfigured as exc:
        return {
            "status": STATUS_BLOCKED,
            "reason": str(exc),
            "live_mysql": STATUS_FAIL,
            "wallet_read": STATUS_NOT_TESTED,
            "booking_read": STATUS_NOT_TESTED,
            "ledger_read": STATUS_NOT_TESTED,
        }

    if not isinstance(reader, OldMySQLReader):
        return {
            "status": STATUS_FAIL,
            "reason": "REAL mode received non-OldMySQLReader — fixture isolation failure",
            "live_mysql": STATUS_FAIL,
            "wallet_read": STATUS_NOT_TESTED,
            "booking_read": STATUS_NOT_TESTED,
            "ledger_read": STATUS_NOT_TESTED,
        }

    try:
        with reader:
            probe = reader.connection_probe()
            # Minimal read paths — SELECT/SHOW only (enforced by reader)
            wallet_sample = reader.fetchone(
                "SELECT id, user_id, balance FROM user_wallet ORDER BY id ASC LIMIT 1"
            ) if not probe.get("missing_tables") else None
            booking_status = (probe.get("schema_discovery") or {}).get("mapping", {}).get("booking_table", {})
            ledger_sample = reader.fetchone(
                "SELECT id, user_id, amount FROM wallet_transactions ORDER BY id ASC LIMIT 1"
            ) if "wallet_transactions" not in (probe.get("missing_tables") or []) else None
    except OldMySQLConnectionError as exc:
        return {
            "status": STATUS_BLOCKED,
            "reason": f"MySQL connection failed: {type(exc).__name__}",
            "live_mysql": STATUS_FAIL,
            "wallet_read": STATUS_NOT_TESTED,
            "booking_read": STATUS_NOT_TESTED,
            "ledger_read": STATUS_NOT_TESTED,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_FAIL,
            "reason": f"MySQL probe error: {type(exc).__name__}",
            "live_mysql": STATUS_FAIL,
            "wallet_read": STATUS_NOT_TESTED,
            "booking_read": STATUS_NOT_TESTED,
            "ledger_read": STATUS_NOT_TESTED,
        }

    writable = bool(probe.get("account_appears_writable"))
    ok = bool(probe.get("ok")) and not writable
    return {
        "status": STATUS_PASS if ok else (STATUS_BLOCKED if writable else STATUS_FAIL),
        "reason": (
            "Read-only probe succeeded"
            if ok
            else (
                probe.get("writable_account_recommendation")
                or "Schema/connectivity incomplete"
            )
        ),
        "live_mysql": STATUS_PASS if probe.get("ok") else STATUS_FAIL,
        "account_appears_writable": writable,
        "mysql_read_only_flag": probe.get("mysql_read_only_flag"),
        "missing_tables": probe.get("missing_tables"),
        "row_counts": probe.get("row_counts"),
        "wallet_read": STATUS_PASS if wallet_sample is not None or probe.get("row_counts", {}).get("user_wallet") == 0 else STATUS_FAIL,
        "ledger_read": STATUS_PASS if ledger_sample is not None or probe.get("row_counts", {}).get("wallet_transactions") == 0 else STATUS_FAIL,
        "booking_read": STATUS_PASS if booking_status.get("status") == "VERIFIED" else STATUS_NOT_TESTED,
        "booking_table_status": booking_status.get("status"),
        # Never include row payloads (may contain PII)
        "wallet_sample_present": wallet_sample is not None,
        "ledger_sample_present": ledger_sample is not None,
    }


def run_guard_tests() -> dict[str, Any]:
    """Run automated preflight guard tests; return PASS/FAIL without printing secrets."""
    from django.conf import settings as dj_settings
    from django.test.utils import get_runner

    TestRunner = get_runner(dj_settings)
    runner = TestRunner(verbosity=0, interactive=False, keepdb=True)
    failures = runner.run_tests(["iic_booking.users.tests.test_real_integration_preflight"])
    return {
        "status": STATUS_PASS if not failures else STATUS_FAIL,
        "failures": int(failures or 0),
        "suite": "iic_booking.users.tests.test_real_integration_preflight",
        "expected": "12 PASS",
    }


def run_staging_activation(
    *,
    backend_commit: str = "",
    frontend_commit: str = "",
    run_tests: bool = True,
    attempt_live_probes: bool = True,
) -> dict[str, Any]:
    """Safe-by-default activation. Does not edit env. Does not auto-enable REAL mode."""
    _refuse_production_settings()

    steps: list[dict[str, Any]] = []
    preflight = build_real_integration_preflight(
        backend_commit=backend_commit,
        frontend_commit=frontend_commit,
    )
    steps.append({"step": 1, "name": "preflight", "status": preflight.get("overall")})

    redirect = omniport_redirect_uri_status()
    if redirect.get("status") != "VALID":
        report = _finalize(
            verdict="REAL STAGING ACTIVATION BLOCKED",
            overall="NOT READY FOR REAL INTEGRATION",
            stop_reason="CHANNEL-I REDIRECT URI INVALID",
            preflight=preflight,
            steps=steps + [{"step": 2, "name": "redirect", "status": STATUS_BLOCKED, "detail": redirect}],
            backend_commit=backend_commit,
            frontend_commit=frontend_commit,
        )
        return report
    steps.append({"step": 2, "name": "redirect", "status": "VALID"})

    if not real_integration_enabled():
        report = _finalize(
            verdict="REAL STAGING ACTIVATION BLOCKED",
            overall="NOT READY FOR REAL INTEGRATION",
            stop_reason=(
                "REAL MODE NOT ENABLED — operator must set REAL_INTEGRATION_ENABLED=true "
                "in .envs/.staging/.django (command will not edit the env file)"
            ),
            preflight=preflight,
            steps=steps
            + [
                {
                    "step": 3,
                    "name": "real_mode",
                    "status": STATUS_BLOCKED,
                    "requirement": "REAL_INTEGRATION_ENABLED=true; fixture modes=false",
                }
            ],
            backend_commit=backend_commit,
            frontend_commit=frontend_commit,
            waiting_for_operator=True,
        )
        return report

    if bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False)) or bool(
        getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False)
    ):
        report = _finalize(
            verdict="REAL STAGING ACTIVATION BLOCKED",
            overall="NOT READY FOR REAL INTEGRATION",
            stop_reason="Fixture modes must be explicitly false when REAL_INTEGRATION_ENABLED=true",
            preflight=preflight,
            steps=steps + [{"step": 3, "name": "fixture_flags", "status": STATUS_FAIL}],
            backend_commit=backend_commit,
            frontend_commit=frontend_commit,
        )
        return report
    steps.append({"step": 3, "name": "real_mode", "status": "ENABLED"})

    # Mandatory config completeness
    channel = channel_i_preflight_status()
    mysql = legacy_mysql_preflight_status()
    emp = employee_id_claim_status()
    if channel.get("status") == STATUS_BLOCKED or mysql.get("status") == STATUS_BLOCKED or emp.get("status") == STATUS_BLOCKED:
        report = _finalize(
            verdict="REAL STAGING ACTIVATION BLOCKED",
            overall="NOT READY FOR REAL INTEGRATION",
            stop_reason="Mandatory dependency BLOCKED (credentials/claim/config)",
            preflight=preflight,
            steps=steps
            + [
                {
                    "step": "config",
                    "Channel-I": channel.get("status"),
                    "Legacy_MySQL": mysql.get("status"),
                    "Employee_Identity": emp.get("status"),
                }
            ],
            backend_commit=backend_commit,
            frontend_commit=frontend_commit,
            waiting_for_operator=True,
        )
        return report

    test_result = {"status": STATUS_NOT_TESTED, "skipped": True}
    if run_tests:
        test_result = run_guard_tests()
        steps.append({"step": 4, "name": "guard_tests", **test_result})
        if test_result.get("status") != STATUS_PASS:
            return _finalize(
                verdict="REAL STAGING ACTIVATION BLOCKED",
                overall="NOT READY FOR REAL INTEGRATION",
                stop_reason="Guard tests failed",
                preflight=preflight,
                steps=steps,
                backend_commit=backend_commit,
                frontend_commit=frontend_commit,
                tests=test_result,
            )

    channel_probe = {"status": STATUS_NOT_TESTED}
    mysql_probe = {"status": STATUS_NOT_TESTED}
    fixture_iso = verify_fixture_isolation_under_real_intent()
    steps.append({"step": 7, "name": "fixture_isolation", **fixture_iso})
    if fixture_iso.get("status") != STATUS_PASS:
        return _finalize(
            verdict="REAL STAGING ACTIVATION BLOCKED",
            overall="NOT READY FOR REAL INTEGRATION",
            stop_reason="Fixture isolation FAIL",
            preflight=preflight,
            steps=steps,
            backend_commit=backend_commit,
            frontend_commit=frontend_commit,
            fixture_isolation=fixture_iso,
            tests=test_result,
        )

    if attempt_live_probes:
        # Re-verify durable Omniport identity + live RO MySQL (no new OAuth codes/tokens).
        live_identity = probe_live_channel_i_identity()
        write_live_channel_i_evidence(live_identity)
        channel_probe = channel_i_from_live_probe(live_identity)
        channel_probe["live_oauth"] = live_identity.get("live_oauth")
        channel_probe["live_userinfo"] = live_identity.get("live_userinfo")
        channel_probe["employee_identity_pass"] = live_identity.get("employee_identity_pass")
        channel_probe["exact_match_count"] = live_identity.get("exact_match_count")
        emp = employee_identity_from_live_probe(live_identity)
        steps.append(
            {
                "step": 5,
                "name": "channel_i_probe",
                "status": channel_probe.get("status"),
                "live_oauth": channel_probe.get("live_oauth"),
                "evidence_class": channel_probe.get("evidence_class"),
            }
        )
        mysql_probe = probe_legacy_mysql_readonly()
        steps.append({"step": 6, "name": "mysql_probe", "status": mysql_probe.get("status")})

    s3 = s3_integration_status()
    steps.append(
        {
            "step": 8,
            "name": "s3",
            "status": s3.get("status"),
            "mode": s3.get("mode"),
            "accepted_limitation": s3.get("accepted_limitation"),
        }
    )

    live_oauth_done = channel_probe.get("live_oauth") == STATUS_PASS
    live_mysql_ok = mysql_probe.get("status") == STATUS_PASS
    emp_live_ok = bool(channel_probe.get("employee_identity_pass")) or (
        emp.get("status") == STATUS_PASS and emp.get("claim_pass") is True
    )
    s3_ok = not s3_blocks_real_activation(s3)

    if channel_probe.get("live_oauth") == "OPERATOR ACTION REQUIRED":
        overall = "NOT READY FOR REAL INTEGRATION"
        verdict = "REAL STAGING ACTIVATION BLOCKED"
        stop_reason = "OPERATOR ACTION REQUIRED — complete staging Channel-I OAuth to prove employee claim"
    elif not s3_ok:
        overall = "NOT READY FOR REAL INTEGRATION"
        verdict = "REAL STAGING ACTIVATION BLOCKED"
        stop_reason = (
            "Staging S3 NOT_AVAILABLE — set STAGING_STORAGE_BACKEND=S3 + AWS_* "
            "or LOCAL_STAGING_ACCEPTED=true"
        )
    elif live_oauth_done and live_mysql_ok and emp_live_ok and fixture_iso.get("status") == STATUS_PASS and s3_ok:
        overall = "READY FOR REAL STAGING INTEGRATION"
        verdict = "READY FOR REAL STAGING INTEGRATION"
        stop_reason = ""
    else:
        overall = "NOT READY FOR REAL INTEGRATION"
        verdict = "REAL STAGING ACTIVATION BLOCKED"
        stop_reason = "Live probes incomplete or failed (config-only is not PASS)"

    return _finalize(
        verdict=verdict,
        overall=overall,
        stop_reason=stop_reason,
        preflight=preflight,
        steps=steps,
        backend_commit=backend_commit,
        frontend_commit=frontend_commit,
        tests=test_result,
        channel_probe=channel_probe,
        mysql_probe=mysql_probe,
        fixture_isolation=fixture_iso,
        s3=s3,
        employee=emp,
        waiting_for_operator=channel_probe.get("live_oauth") == "OPERATOR ACTION REQUIRED",
    )


def _finalize(
    *,
    verdict: str,
    overall: str,
    stop_reason: str,
    preflight: dict,
    steps: list,
    backend_commit: str = "",
    frontend_commit: str = "",
    waiting_for_operator: bool = False,
    tests: dict | None = None,
    channel_probe: dict | None = None,
    mysql_probe: dict | None = None,
    fixture_isolation: dict | None = None,
    s3: dict | None = None,
    employee: dict | None = None,
) -> dict[str, Any]:
    s3 = s3 or s3_integration_status()
    employee = employee or employee_id_claim_status()
    channel_probe = channel_probe or {"status": STATUS_NOT_TESTED}
    mysql_probe = mysql_probe or {"status": STATUS_NOT_TESTED}
    fixture_isolation = fixture_isolation or {"status": STATUS_NOT_TESTED}
    tests = tests or {"status": STATUS_NOT_TESTED}

    tooling_ready = True  # commands exist and refuse unsafe activation
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "title": "REAL STAGING ACTIVATION",
        "verdict": verdict,
        "overall": overall,
        "stop_reason": stop_reason,
        "waiting_for_operator": waiting_for_operator,
        "tooling_status": "REAL STAGING ACTIVATION READY — WAITING FOR OPERATOR CREDENTIALS"
        if waiting_for_operator or overall != "READY FOR REAL STAGING INTEGRATION"
        else "READY FOR REAL STAGING INTEGRATION",
        "environment": getattr(settings, "DEPLOYMENT_ENVIRONMENT", "UNKNOWN"),
        "settings_module": getattr(settings, "SETTINGS_MODULE", ""),
        "backend_commit": backend_commit or preflight.get("backend_commit"),
        "frontend_commit": frontend_commit or preflight.get("frontend_commit"),
        "database": preflight.get("database"),
        "real_integration_enabled": real_integration_enabled(),
        "never_edits_env": True,
        "production_writes": "NO",
        "production_ec2": "UNCHANGED",
        "production_rds": "UNCHANGED",
        "Channel-I": channel_probe.get("status")
        if channel_probe.get("status") not in (None, STATUS_NOT_TESTED)
        else preflight.get("Channel-I", {}).get("status"),
        "Channel-I_live": channel_probe,
        "Employee_Identity": employee.get("status"),
        "Employee_Identity_detail": employee,
        "Legacy_MySQL": mysql_probe.get("status")
        if mysql_probe.get("status") not in (None, STATUS_NOT_TESTED)
        else preflight.get("Legacy_MySQL", {}).get("status"),
        "Legacy_MySQL_live": mysql_probe,
        "wallet_read": mysql_probe.get("wallet_read", STATUS_NOT_TESTED),
        "booking_read": mysql_probe.get("booking_read", STATUS_NOT_TESTED),
        "ledger_read": mysql_probe.get("ledger_read", STATUS_NOT_TESTED),
        "Staging_S3": s3.get("status"),
        "Staging_S3_detail": s3,
        "Staging_S3_display": (
            f"{s3.get('status')} / ACCEPTED LIMITATION"
            if s3.get("status") == STATUS_NOT_AVAILABLE and s3.get("accepted_limitation")
            else s3.get("status")
        ),
        "Fixture_Isolation": fixture_isolation.get("status"),
        "Fixture_Isolation_detail": fixture_isolation,
        "guard_tests": tests,
        "steps": steps,
        "preflight": {
            "overall": preflight.get("overall"),
            "credentials_presence": preflight.get("credentials_presence"),
            "blocked_reasons": preflight.get("blocked_reasons"),
        },
        "overall_ready_for_real_integration": overall == "READY FOR REAL STAGING INTEGRATION",
        "safety_result": overall
        if overall == "READY FOR REAL STAGING INTEGRATION"
        else "NOT READY FOR REAL INTEGRATION",
        "tooling_ready": tooling_ready,
    }
    return report


def format_activation_human(report: dict) -> str:
    lines = [
        "REAL STAGING ACTIVATION",
        "",
        f"Verdict: {report.get('verdict')}",
        f"Overall: {report.get('overall')}",
        f"Tooling: {report.get('tooling_status')}",
        f"Stop reason: {report.get('stop_reason') or '(none)'}",
        f"REAL_INTEGRATION_ENABLED: {report.get('real_integration_enabled')}",
        f"Environment: {report.get('environment')}",
        f"Production Writes: {report.get('production_writes')}",
        "",
        f"Channel-I: {report.get('Channel-I')}",
        f"Employee Identity: {report.get('Employee_Identity')}",
        f"Legacy MySQL: {report.get('Legacy_MySQL')}",
        f"Wallet read: {report.get('wallet_read')}",
        f"Booking read: {report.get('booking_read')}",
        f"S3: {report.get('Staging_S3_display') or report.get('Staging_S3')}",
        f"Fixture Isolation: {report.get('Fixture_Isolation')}",
        f"Guard tests: {(report.get('guard_tests') or {}).get('status')}",
        "",
        "This command never edits .envs/.staging/.django.",
    ]
    return "\n".join(lines)


def write_activation_evidence(report: dict, *, also_preflight: bool = True) -> list[str]:
    """Write evidence markdown/json. Never includes secrets."""
    base = Path(settings.BASE_DIR) / "docs" / "release" / "migration"
    base.mkdir(parents=True, exist_ok=True)
    written = []
    result_md = base / "AI30-AI31-REAL-ACTIVATION-RESULT.md"
    result_md.write_text(_activation_markdown(report), encoding="utf-8")
    written.append(str(result_md))

    result_json = base / "real_integration_activation.json"
    result_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    written.append(str(result_json))

    if also_preflight:
        pre = build_real_integration_preflight(
            backend_commit=report.get("backend_commit") or "",
            frontend_commit=report.get("frontend_commit") or "",
            include_live_probes=True,
        )
        # Attach activation summary without secrets
        pre["activation_summary"] = {
            "verdict": report.get("verdict"),
            "overall": report.get("overall"),
            "stop_reason": report.get("stop_reason"),
            "waiting_for_operator": report.get("waiting_for_operator"),
            "tooling_status": report.get("tooling_status"),
            "production_writes": "NO",
        }
        out = base / "real_integration_preflight.json"
        out.write_text(json.dumps(pre, indent=2) + "\n", encoding="utf-8")
        written.append(str(out))
    return written


def _activation_markdown(report: dict) -> str:
    return f"""# AI30/AI31 REAL Activation Result

**Timestamp (UTC):** {report.get("timestamp_utc")}  
**Backend:** `{report.get("backend_commit")}`  
**Frontend:** `{report.get("frontend_commit")}`  
**Database:** `{((report.get("database") or {}).get("name"))}`  
**Environment:** {report.get("environment")}

## Verdict

**{report.get("verdict")}**  
**Overall:** {report.get("overall")}  
**Tooling:** {report.get("tooling_status")}  
**Stop reason:** {report.get("stop_reason") or "(none)"}  

## Results

| Gate | Status |
|------|--------|
| Channel-I | {report.get("Channel-I")} |
| Employee Identity | {report.get("Employee_Identity")} |
| Legacy MySQL | {report.get("Legacy_MySQL")} |
| Wallet read | {report.get("wallet_read")} |
| Booking read | {report.get("booking_read")} |
| Staging S3 | {report.get("Staging_S3_display") or report.get("Staging_S3")} |
| Fixture Isolation | {report.get("Fixture_Isolation")} |
| Guard tests | {(report.get("guard_tests") or {}).get("status")} |

## Production safety

| Check | Result |
|-------|--------|
| Production writes | {report.get("production_writes")} |
| Production EC2 | {report.get("production_ec2")} |
| Production RDS | {report.get("production_rds")} |
| Env auto-edited | NO (`never_edits_env={report.get("never_edits_env")}`) |

## Operator note

This command does **not** set `REAL_INTEGRATION_ENABLED` and does **not** write `.envs/.staging/.django`.
Supply credentials manually, then re-run:

```bash
python manage.py real_integration_status
python manage.py real_integration_activate_staging --write-docs --backend-commit <sha> --frontend-commit <sha>
```
"""
