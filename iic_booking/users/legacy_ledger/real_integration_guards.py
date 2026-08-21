"""Guards and deterministic preflight for REAL vs FIXTURE staging integration.

Never prints secret values. Never invents credentials. Never weakens REAL checks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

STATUS_PASS = "PASS"
STATUS_BLOCKED = "BLOCKED"
STATUS_NOT_AVAILABLE = "NOT_AVAILABLE"
STATUS_NOT_TESTED = "NOT_TESTED"
STATUS_FAIL = "FAIL"
STATUS_CONFIGURED = "CONFIGURED"
STATUS_PRESENT = "PRESENT"
STATUS_ABSENT = "ABSENT"
STATUS_READY = "READY"
STATUS_INVALID = "INVALID"
STATUS_VALID = "VALID"

# Canonical REAL integration Omniport callback (do not accept legacy channel-i path).
EXPECTED_OMNIPORT_CALLBACK_PATH = "/api/auth/omniport/callback/"
LEGACY_WRONG_CALLBACK_MARKER = "/api/v1/auth/channel-i/callback/"

# Operator must set one of these explicitly — no silent email/name fallback.
ALLOWED_EMPLOYEE_ID_CLAIMS = frozenset(
    {
        "operator_confirmed_map",
        "username",
        "student.enrolmentNumber",
        "student.enrolment_number",
        "facultyMember.employeeId",
    }
)


def _present(value: Any) -> bool:
    return bool(str(value or "").strip())


def _presence_label(value: Any) -> str:
    return STATUS_PRESENT if _present(value) else STATUS_ABSENT


def real_integration_enabled() -> bool:
    """Explicit operator intent for live staging dependencies (not fixtures)."""
    return bool(getattr(settings, "REAL_INTEGRATION_ENABLED", False))


def omniport_redirect_uri_status() -> dict[str, Any]:
    """Validate OMNIPORT_REDIRECT_URI path without printing full secrets."""
    redirect = (getattr(settings, "OMNIPORT_REDIRECT_URI", "") or "").strip()
    if not redirect:
        return {
            "status": STATUS_INVALID,
            "reason": "OMNIPORT_REDIRECT_URI absent",
            "expected_path": EXPECTED_OMNIPORT_CALLBACK_PATH,
            "path": "",
            "matches_expected": False,
            "is_legacy_wrong_path": False,
        }
    parsed = urlparse(redirect)
    path = parsed.path or ""
    if not path.endswith("/"):
        path = path + "/"
    # Normalize double slashes in path only (keep scheme:// intact via urlparse).
    while "//" in path:
        path = path.replace("//", "/")
    is_legacy = LEGACY_WRONG_CALLBACK_MARKER.rstrip("/") in redirect or path.rstrip(
        "/"
    ).endswith("/api/v1/auth/channel-i/callback")
    matches = path == EXPECTED_OMNIPORT_CALLBACK_PATH or path.endswith(
        EXPECTED_OMNIPORT_CALLBACK_PATH
    )
    if is_legacy or not matches:
        return {
            "status": STATUS_INVALID,
            "reason": "CHANNEL-I REDIRECT URI INVALID",
            "expected_path": EXPECTED_OMNIPORT_CALLBACK_PATH,
            "path": path,
            "matches_expected": False,
            "is_legacy_wrong_path": is_legacy,
            "note": (
                "Do not use /api/v1/auth/channel-i/callback/. "
                f"Required path: {EXPECTED_OMNIPORT_CALLBACK_PATH}"
            ),
        }
    return {
        "status": STATUS_VALID,
        "reason": "Redirect URI path matches expected Omniport callback",
        "expected_path": EXPECTED_OMNIPORT_CALLBACK_PATH,
        "path": path,
        "matches_expected": True,
        "is_legacy_wrong_path": False,
    }


def assert_omniport_redirect_uri_valid() -> dict[str, Any]:
    status = omniport_redirect_uri_status()
    if status["status"] != STATUS_VALID:
        raise ImproperlyConfigured(
            status.get("reason")
            or f"CHANNEL-I REDIRECT URI INVALID — required {EXPECTED_OMNIPORT_CALLBACK_PATH}"
        )
    return status


def assert_staging_environment() -> None:
    env = (getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "").upper()
    if env != "STAGING":
        raise ImproperlyConfigured(
            f"REAL integration activation requires DEPLOYMENT_ENVIRONMENT=STAGING (got {env!r})."
        )
    host = str(settings.DATABASES.get("default", {}).get("HOST") or "")
    if "iic-booking-rds" in host:
        raise ImproperlyConfigured("SAFETY STOP: database host looks like production RDS.")


def assert_real_channel_i_ready() -> dict:
    """Return presence map; raise if REAL Channel-I cannot run."""
    assert_staging_environment()
    if bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False)):
        raise ImproperlyConfigured(
            "CHANNEL_I_STAGING_FIXTURE_MODE is still true. Disable fixture mode before "
            "claiming REAL Channel-I integration."
        )
    client_id = (getattr(settings, "OMNIPORT_CLIENT_ID", "") or "").strip()
    client_secret = (getattr(settings, "OMNIPORT_CLIENT_SECRET", "") or "").strip()
    if not client_id or not client_secret:
        raise ImproperlyConfigured(
            "REAL Channel-I credentials missing: set OMNIPORT_CLIENT_ID and "
            "OMNIPORT_CLIENT_SECRET. Staging will not invent or reuse production secrets."
        )
    redirect_status = assert_omniport_redirect_uri_valid()
    return {
        "client_id_present": True,
        "client_secret_present": True,
        "redirect_uri_path": redirect_status.get("path"),
        "fixture_mode": False,
        "mode": "REAL",
    }


def assert_real_legacy_mysql_ready() -> dict:
    assert_staging_environment()
    if bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False)):
        raise ImproperlyConfigured(
            "LEGACY_MYSQL_STAGING_FIXTURE_MODE is still true. Disable fixture mode before "
            "claiming REAL legacy MySQL integration."
        )
    host = (getattr(settings, "OLD_MYSQL_HOST", "") or "").strip()
    user = (getattr(settings, "OLD_MYSQL_USER", "") or "").strip()
    database = (getattr(settings, "OLD_MYSQL_DATABASE", "") or "").strip()
    password = getattr(settings, "OLD_MYSQL_PASSWORD", None) or ""
    if not host or not user or not database or not str(password).strip():
        raise ImproperlyConfigured(
            "REAL legacy MySQL not configured. Set OLD_MYSQL_HOST, OLD_MYSQL_USER, "
            "OLD_MYSQL_DATABASE, and OLD_MYSQL_PASSWORD. No silent fixture fallback."
        )
    return {
        "host_present": True,
        "user_present": True,
        "database_present": True,
        "password_present": True,
        "fixture_mode": False,
        "mode": "REAL",
    }


def local_staging_accepted() -> bool:
    """Operator may formally accept LOCAL_STAGING as a staging limitation (never a PASS)."""
    return bool(getattr(settings, "LOCAL_STAGING_ACCEPTED", False))


def s3_integration_status() -> dict:
    backend = (getattr(settings, "STAGING_STORAGE_BACKEND", "") or "LOCAL_STAGING").upper()
    use_s3 = bool(getattr(settings, "USE_S3_MEDIA", False)) or backend == "S3"
    bucket = (getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or "").strip()
    key = (getattr(settings, "AWS_ACCESS_KEY_ID", "") or "").strip()
    secret = (getattr(settings, "AWS_SECRET_ACCESS_KEY", "") or "").strip()
    accepted = local_staging_accepted()
    if use_s3 and bucket and key and secret:
        return {
            "mode": "REAL",
            "status": STATUS_CONFIGURED,
            "bucket_present": True,
            "access_key_present": True,
            "secret_present": True,
            "accepted_limitation": False,
            "claim_pass": False,  # CONFIGURED ≠ live write/read PASS until tested
            "note": "Credentials present; live S3 probe still required before PASS.",
        }
    if accepted:
        note = (
            "S3 = NOT_AVAILABLE / ACCEPTED LIMITATION (LOCAL_STAGING_ACCEPTED=true). "
            "LOCAL_STAGING is not a PASS; real S3 remains unproven."
        )
    else:
        note = (
            "S3 REAL INTEGRATION = NOT_AVAILABLE; LOCAL_STAGING retained. Not a PASS. "
            "AWS_* PRESENT alone does not count — STAGING_STORAGE_BACKEND=S3 and "
            "USE_S3_MEDIA=True are required for staging S3 mode. "
            "Or set LOCAL_STAGING_ACCEPTED=true to formally accept this limitation."
            if (bucket or key or secret)
            else (
                "S3 REAL INTEGRATION = NOT_AVAILABLE; LOCAL_STAGING retained. Not a PASS. "
                "Set STAGING_STORAGE_BACKEND=S3 + USE_S3_MEDIA=True + AWS_*, "
                "or LOCAL_STAGING_ACCEPTED=true to formally accept this limitation."
            )
        )
    return {
        "mode": "LOCAL_STAGING",
        "status": STATUS_NOT_AVAILABLE,
        "bucket_present": bool(bucket),
        "access_key_present": bool(key),
        "secret_present": bool(secret),
        "accepted_limitation": accepted,
        "claim_pass": False,
        "note": note,
    }


def s3_blocks_real_activation(s3: dict | None = None) -> bool:
    """S3 blocks GO only when NOT_AVAILABLE and not formally accepted."""
    s3 = s3 or s3_integration_status()
    if s3.get("status") != STATUS_NOT_AVAILABLE:
        return False
    return not bool(s3.get("accepted_limitation"))


def employee_id_claim_status() -> dict:
    claim = (getattr(settings, "CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM", "") or "").strip()
    if not claim:
        return {
            "status": STATUS_BLOCKED,
            "claim": "",
            "reason": "CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM is empty/unproven",
            "wallet_identity": STATUS_BLOCKED,
            "recognized": False,
        }
    if claim not in ALLOWED_EMPLOYEE_ID_CLAIMS:
        return {
            "status": STATUS_BLOCKED,
            "claim": claim,
            "reason": (
                "Unrecognized CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM — "
                "refusing silent email/name fallback. Allowed: "
                + ", ".join(sorted(ALLOWED_EMPLOYEE_ID_CLAIMS))
            ),
            "wallet_identity": STATUS_BLOCKED,
            "recognized": False,
        }
    return {
        "status": STATUS_CONFIGURED,
        "claim": claim,
        "reason": "Claim configured and recognized; live Channel-I mapping still required before PASS",
        "wallet_identity": STATUS_NOT_TESTED,
        "claim_pass": False,
        "recognized": True,
    }


def assert_real_employee_claim_ready() -> dict:
    """When REAL mode is intended, claim must be non-empty and recognized."""
    assert_staging_environment()
    status = employee_id_claim_status()
    if status.get("status") == STATUS_BLOCKED:
        raise ImproperlyConfigured(status.get("reason") or "Employee ID claim BLOCKED")
    return status


def channel_i_preflight_status() -> dict:
    fixture = bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False))
    client_id = _presence_label(getattr(settings, "OMNIPORT_CLIENT_ID", None))
    client_secret = _presence_label(getattr(settings, "OMNIPORT_CLIENT_SECRET", None))
    redirect = (getattr(settings, "OMNIPORT_REDIRECT_URI", "") or "").strip()
    redirect_status = omniport_redirect_uri_status()
    expected_callback_suffix = EXPECTED_OMNIPORT_CALLBACK_PATH
    redirect_ok = redirect_status.get("matches_expected") is True

    if real_integration_enabled() or (not fixture and (client_id == STATUS_PRESENT or client_secret == STATUS_PRESENT)):
        # REAL intent / partial config path
        if fixture:
            return {
                "status": STATUS_BLOCKED,
                "reason": "CHANNEL_I_STAGING_FIXTURE_MODE=true while REAL integration intended",
                "OMNIPORT_CLIENT_ID": client_id,
                "OMNIPORT_CLIENT_SECRET": client_secret,
                "redirect_uri_configured": bool(redirect),
                "redirect_uri_matches_expected_callback": redirect_ok,
                "expected_callback": expected_callback_suffix,
                "mode": "AMBIGUOUS",
                "evidence_class": "NOT_TESTED",
            }
        if client_id != STATUS_PRESENT or client_secret != STATUS_PRESENT:
            reason = "MISSING_CREDENTIALS"
            if redirect and not redirect_ok:
                reason = (
                    "MISSING_CREDENTIALS; CHANNEL-I REDIRECT URI INVALID — "
                    f"required {expected_callback_suffix}"
                )
            return {
                "status": STATUS_BLOCKED,
                "reason": reason,
                "OMNIPORT_CLIENT_ID": client_id,
                "OMNIPORT_CLIENT_SECRET": client_secret,
                "redirect_uri_configured": bool(redirect),
                "redirect_uri_matches_expected_callback": redirect_ok,
                "expected_callback": expected_callback_suffix,
                "mode": "REAL_REQUESTED",
                "evidence_class": "NOT_TESTED",
            }
        if not redirect_ok:
            return {
                "status": STATUS_BLOCKED,
                "reason": "CHANNEL-I REDIRECT URI INVALID",
                "OMNIPORT_CLIENT_ID": client_id,
                "OMNIPORT_CLIENT_SECRET": client_secret,
                "redirect_uri_configured": bool(redirect),
                "redirect_uri_matches_expected_callback": False,
                "expected_callback": expected_callback_suffix,
                "mode": "REAL_REQUESTED",
                "evidence_class": "NOT_TESTED",
            }
        return {
            "status": STATUS_CONFIGURED,
            "reason": "Credentials present; live OAuth probe still required before PASS",
            "OMNIPORT_CLIENT_ID": client_id,
            "OMNIPORT_CLIENT_SECRET": client_secret,
            "redirect_uri_configured": bool(redirect),
            "redirect_uri_matches_expected_callback": redirect_ok,
            "expected_callback": expected_callback_suffix,
            "mode": "REAL",
            "evidence_class": "NOT_TESTED",
            "claim_pass": False,
        }

    if fixture:
        return {
            "status": STATUS_NOT_TESTED,
            "reason": "FIXTURE mode explicit — not REAL evidence",
            "OMNIPORT_CLIENT_ID": client_id,
            "OMNIPORT_CLIENT_SECRET": client_secret,
            "mode": "FIXTURE",
            "evidence_class": "FIXTURE",
        }

    reason = "MISSING_CREDENTIALS"
    if redirect and not redirect_ok:
        reason = (
            "MISSING_CREDENTIALS; CHANNEL-I REDIRECT URI INVALID — "
            f"required {expected_callback_suffix}"
        )
    return {
        "status": STATUS_BLOCKED,
        "reason": reason,
        "OMNIPORT_CLIENT_ID": client_id,
        "OMNIPORT_CLIENT_SECRET": client_secret,
        "redirect_uri_configured": bool(redirect),
        "redirect_uri_matches_expected_callback": redirect_ok,
        "expected_callback": expected_callback_suffix,
        "mode": "UNCONFIGURED",
        "evidence_class": "NOT_TESTED",
    }


def legacy_mysql_preflight_status() -> dict:
    fixture = bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False))
    host = _presence_label(getattr(settings, "OLD_MYSQL_HOST", None))
    port = _presence_label(str(getattr(settings, "OLD_MYSQL_PORT", "") or ""))
    database = _presence_label(getattr(settings, "OLD_MYSQL_DATABASE", None))
    user = _presence_label(getattr(settings, "OLD_MYSQL_USER", None))
    password = _presence_label(getattr(settings, "OLD_MYSQL_PASSWORD", None))
    real_intent = real_integration_enabled() or host == STATUS_PRESENT

    if real_intent and fixture:
        return {
            "status": STATUS_BLOCKED,
            "reason": "LEGACY_MYSQL_STAGING_FIXTURE_MODE=true while REAL MySQL intended — refusing fixture fallback",
            "OLD_MYSQL_HOST": host,
            "OLD_MYSQL_PORT": port,
            "OLD_MYSQL_DATABASE": database,
            "OLD_MYSQL_USER": user,
            "OLD_MYSQL_PASSWORD": password,
            "mode": "AMBIGUOUS",
            "evidence_class": "NOT_TESTED",
        }

    if real_intent:
        missing = [
            name
            for name, label in (
                ("OLD_MYSQL_HOST", host),
                ("OLD_MYSQL_DATABASE", database),
                ("OLD_MYSQL_USER", user),
                ("OLD_MYSQL_PASSWORD", password),
            )
            if label != STATUS_PRESENT
        ]
        if missing:
            return {
                "status": STATUS_BLOCKED,
                "reason": "MISSING_CREDENTIALS",
                "missing": missing,
                "OLD_MYSQL_HOST": host,
                "OLD_MYSQL_PORT": port,
                "OLD_MYSQL_DATABASE": database,
                "OLD_MYSQL_USER": user,
                "OLD_MYSQL_PASSWORD": password,
                "mode": "REAL_REQUESTED",
                "evidence_class": "NOT_TESTED",
                "read_only_required": True,
            }
        return {
            "status": STATUS_CONFIGURED,
            "reason": "Credentials present; live read-only probe still required before PASS",
            "OLD_MYSQL_HOST": host,
            "OLD_MYSQL_PORT": port,
            "OLD_MYSQL_DATABASE": database,
            "OLD_MYSQL_USER": user,
            "OLD_MYSQL_PASSWORD": password,
            "mode": "REAL",
            "evidence_class": "NOT_TESTED",
            "read_only_required": True,
            "claim_pass": False,
        }

    if fixture:
        return {
            "status": STATUS_NOT_TESTED,
            "reason": "FIXTURE/SIMULATED mode explicit — not REAL evidence",
            "OLD_MYSQL_HOST": host,
            "OLD_MYSQL_PASSWORD": password,
            "mode": "FIXTURE",
            "evidence_class": "FIXTURE",
        }

    return {
        "status": STATUS_BLOCKED,
        "reason": "MISSING_CREDENTIALS",
        "OLD_MYSQL_HOST": host,
        "OLD_MYSQL_PORT": port,
        "OLD_MYSQL_DATABASE": database,
        "OLD_MYSQL_USER": user,
        "OLD_MYSQL_PASSWORD": password,
        "mode": "UNCONFIGURED",
        "evidence_class": "NOT_TESTED",
        "read_only_required": True,
    }


def build_real_integration_preflight(
    *,
    backend_commit: str = "",
    frontend_commit: str = "",
    include_live_probes: bool = False,
) -> dict:
    """Machine-readable preflight. Never includes secret values.

    When include_live_probes=True (operator/preflight --write-docs / activate),
    re-verifies durable Channel-I identity + live RO MySQL and upgrades PASS only
    when those live conditions hold. CONFIGURED alone never becomes PASS.
    """
    assert_staging_environment()
    channel_i = channel_i_preflight_status()
    mysql = legacy_mysql_preflight_status()
    s3 = s3_integration_status()
    emp = employee_id_claim_status()
    live_channel = None
    live_mysql = None

    if include_live_probes:
        from iic_booking.users.legacy_ledger.real_integration_activation import (
            probe_legacy_mysql_readonly,
        )
        from iic_booking.users.legacy_ledger.real_integration_live_evidence import (
            channel_i_from_live_probe,
            employee_identity_from_live_probe,
            probe_live_channel_i_identity,
            write_live_channel_i_evidence,
        )

        live_channel = probe_live_channel_i_identity()
        channel_i = channel_i_from_live_probe(live_channel, channel_i)
        emp = employee_identity_from_live_probe(live_channel)
        write_live_channel_i_evidence(live_channel)

        if mysql.get("status") == STATUS_CONFIGURED:
            live_mysql = probe_legacy_mysql_readonly()
            if live_mysql.get("status") == STATUS_PASS:
                mysql = {
                    **mysql,
                    "status": STATUS_PASS,
                    "evidence_class": "REAL",
                    "claim_pass": True,
                    "reason": live_mysql.get("reason") or "Live read-only probe succeeded",
                    "live_mysql": live_mysql.get("live_mysql"),
                    "wallet_read": live_mysql.get("wallet_read"),
                    "ledger_read": live_mysql.get("ledger_read"),
                    "booking_read": live_mysql.get("booking_read"),
                    "row_counts": live_mysql.get("row_counts"),
                }
            elif live_mysql.get("status") in (STATUS_BLOCKED, STATUS_FAIL):
                mysql = {
                    **mysql,
                    "status": live_mysql.get("status"),
                    "evidence_class": "REAL",
                    "claim_pass": False,
                    "reason": live_mysql.get("reason") or mysql.get("reason"),
                }

    mandatory_blocked = [
        ("Channel-I", channel_i),
        ("Legacy MySQL", mysql),
        ("Employee Identity", emp),
    ]
    blocked_reasons = []
    for name, block in mandatory_blocked:
        if block.get("status") in (STATUS_BLOCKED, STATUS_FAIL):
            blocked_reasons.append(f"{name}: {block.get('reason') or block.get('status')}")

    # S3: NOT_AVAILABLE blocks unless operator formally accepts LOCAL_STAGING.
    if real_integration_enabled() and s3_blocks_real_activation(s3):
        blocked_reasons.append(
            "Staging S3: NOT_AVAILABLE (set STAGING_STORAGE_BACKEND=S3 + AWS_* "
            "or LOCAL_STAGING_ACCEPTED=true)"
        )

    live_ok = (
        channel_i.get("status") == STATUS_PASS
        and channel_i.get("evidence_class") == "REAL"
        and mysql.get("status") == STATUS_PASS
        and mysql.get("evidence_class") == "REAL"
        and emp.get("status") == STATUS_PASS
        and emp.get("claim_pass") is True
        and not s3_blocks_real_activation(s3)
    )

    if live_ok and not blocked_reasons and real_integration_enabled():
        overall = "READY FOR REAL STAGING INTEGRATION"
        overall_ready = True
        safety = "READY FOR REAL STAGING INTEGRATION"
    else:
        overall = "NOT READY FOR REAL INTEGRATION"
        overall_ready = False
        safety = "NOT READY FOR REAL INTEGRATION"
        # Never PASS overall from credentials alone
        if include_live_probes:
            if channel_i.get("status") == STATUS_CONFIGURED and channel_i.get("evidence_class") != "REAL":
                blocked_reasons.append(
                    "Channel-I live evidence incomplete — complete Omniport login or re-run live probes"
                )
            if mysql.get("status") == STATUS_CONFIGURED and mysql.get("evidence_class") != "REAL":
                blocked_reasons.append("Legacy MySQL live probe not PASS")
            if emp.get("status") == STATUS_CONFIGURED and not emp.get("claim_pass"):
                blocked_reasons.append("Employee Identity live claim not PASS")
        else:
            cfg_ready = (
                channel_i.get("status") in (STATUS_CONFIGURED, STATUS_PASS)
                and mysql.get("status") in (STATUS_CONFIGURED, STATUS_PASS)
                and emp.get("status") in (STATUS_CONFIGURED, STATUS_PASS)
                and not blocked_reasons
            )
            if cfg_ready:
                blocked_reasons.append(
                    "Live probes NOT_TESTED — credentials/config presence alone is insufficient for PASS"
                )

    db = settings.DATABASES.get("default", {})
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "title": "REAL INTEGRATION PRE-FLIGHT",
        "environment": getattr(settings, "DEPLOYMENT_ENVIRONMENT", "UNKNOWN"),
        "settings_module": getattr(settings, "SETTINGS_MODULE", ""),
        "real_integration_enabled": real_integration_enabled(),
        "production_writes": "NO",
        "backend_commit": backend_commit or "",
        "frontend_commit": frontend_commit or "",
        "database": {
            "name": db.get("NAME"),
            "host": db.get("HOST"),
            "production_rds_marker": "iic-booking-rds" in str(db.get("HOST") or ""),
        },
        "credentials_presence": {
            "OMNIPORT_CLIENT_ID": _presence_label(getattr(settings, "OMNIPORT_CLIENT_ID", None)),
            "OMNIPORT_CLIENT_SECRET": _presence_label(getattr(settings, "OMNIPORT_CLIENT_SECRET", None)),
            "OLD_MYSQL_HOST": _presence_label(getattr(settings, "OLD_MYSQL_HOST", None)),
            "OLD_MYSQL_PORT": _presence_label(str(getattr(settings, "OLD_MYSQL_PORT", "") or "")),
            "OLD_MYSQL_DATABASE": _presence_label(getattr(settings, "OLD_MYSQL_DATABASE", None)),
            "OLD_MYSQL_USER": _presence_label(getattr(settings, "OLD_MYSQL_USER", None)),
            "OLD_MYSQL_PASSWORD": _presence_label(getattr(settings, "OLD_MYSQL_PASSWORD", None)),
            "AWS_ACCESS_KEY_ID": _presence_label(getattr(settings, "AWS_ACCESS_KEY_ID", None)),
            "AWS_SECRET_ACCESS_KEY": _presence_label(getattr(settings, "AWS_SECRET_ACCESS_KEY", None)),
            "AWS_STORAGE_BUCKET_NAME": _presence_label(getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)),
            "CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM": _presence_label(
                getattr(settings, "CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM", None)
            ),
            "LOCAL_STAGING_ACCEPTED": "TRUE" if local_staging_accepted() else "FALSE",
        },
        "Channel-I": channel_i,
        "Legacy_MySQL": mysql,
        "Staging_S3": s3,
        "Employee_Identity": emp,
        "fixture_flags": {
            "CHANNEL_I_STAGING_FIXTURE_MODE": bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False)),
            "LEGACY_MYSQL_STAGING_FIXTURE_MODE": bool(
                getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False)
            ),
        },
        "include_live_probes": include_live_probes,
        "blocked_reasons": blocked_reasons,
        "overall": overall,
        "overall_ready_for_real_integration": overall_ready,
        "safety_result": safety,
    }
    if live_channel is not None:
        report["Channel-I_live"] = live_channel
    if live_mysql is not None:
        report["Legacy_MySQL_live"] = {
            k: live_mysql.get(k)
            for k in (
                "status",
                "live_mysql",
                "wallet_read",
                "ledger_read",
                "booking_read",
                "account_appears_writable",
                "missing_tables",
                "row_counts",
            )
        }
    return report


def format_preflight_human(report: dict) -> str:
    lines = [
        "REAL INTEGRATION PRE-FLIGHT",
        "",
        f"Environment: {report.get('environment')}",
        f"REAL_INTEGRATION_ENABLED: {report.get('real_integration_enabled')}",
        f"Production Writes: {report.get('production_writes')}",
        f"Backend commit: {report.get('backend_commit') or '(unset)'}",
        f"Frontend commit: {report.get('frontend_commit') or '(unset)'}",
        f"Database: {report.get('database', {}).get('name')} @ {report.get('database', {}).get('host')}",
        "",
        "Credentials (presence only):",
    ]
    for k, v in (report.get("credentials_presence") or {}).items():
        lines.append(f"  {k} = {v}")
    lines.extend(
        [
            "",
            "Channel-I:",
            f"  STATUS = {report['Channel-I'].get('status')}",
            f"  Reason: {report['Channel-I'].get('reason')}",
            f"  Mode: {report['Channel-I'].get('mode')}",
            "",
            "Legacy MySQL:",
            f"  STATUS = {report['Legacy_MySQL'].get('status')}",
            f"  Reason: {report['Legacy_MySQL'].get('reason')}",
            f"  Mode: {report['Legacy_MySQL'].get('mode')}",
            "",
            "Staging S3:",
            f"  STATUS = {report['Staging_S3'].get('status')}",
            f"  Reason: {report['Staging_S3'].get('note')}",
            f"  Mode: {report['Staging_S3'].get('mode')}",
            "",
            "Employee Identity:",
            f"  STATUS = {report['Employee_Identity'].get('status')}",
            f"  Reason: {report['Employee_Identity'].get('reason')}",
            "",
            f"Overall: {report.get('overall')}",
        ]
    )
    if report.get("blocked_reasons"):
        lines.append("Blocked reasons:")
        for r in report["blocked_reasons"]:
            lines.append(f"  - {r}")
    return "\n".join(lines)


def _config_ready_label(block: dict, *, configured_statuses=(STATUS_CONFIGURED,)) -> str:
    st = block.get("status")
    if st in (STATUS_BLOCKED, STATUS_FAIL, STATUS_INVALID):
        return STATUS_BLOCKED
    if st == STATUS_NOT_AVAILABLE:
        return STATUS_NOT_AVAILABLE
    if st in configured_statuses or st == STATUS_PASS:
        return STATUS_READY
    if st == STATUS_NOT_TESTED and block.get("mode") == "FIXTURE":
        return STATUS_BLOCKED
    return STATUS_BLOCKED


def build_real_integration_status() -> dict:
    """Lightweight operator status — no live external writes; never prints secrets."""
    assert_staging_environment()
    channel_i = channel_i_preflight_status()
    mysql = legacy_mysql_preflight_status()
    s3 = s3_integration_status()
    emp = employee_id_claim_status()
    redirect = omniport_redirect_uri_status()
    fixture_ci = bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False))
    fixture_my = bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False))
    real_on = real_integration_enabled()

    fixture_isolation = STATUS_PASS
    if real_on and (fixture_ci or fixture_my):
        fixture_isolation = STATUS_FAIL

    channel_label = _config_ready_label(channel_i)
    if redirect.get("status") != STATUS_VALID:
        channel_label = STATUS_BLOCKED
    mysql_label = _config_ready_label(mysql)
    emp_label = _config_ready_label(emp)
    s3_label = STATUS_READY if s3.get("status") == STATUS_CONFIGURED else (
        STATUS_NOT_AVAILABLE if s3.get("status") == STATUS_NOT_AVAILABLE else STATUS_BLOCKED
    )

    blockers = []
    if channel_label != STATUS_READY:
        blockers.append(f"Channel-I: {channel_i.get('reason') or channel_label}")
    if mysql_label != STATUS_READY:
        blockers.append(f"Legacy MySQL: {mysql.get('reason') or mysql_label}")
    if emp_label != STATUS_READY:
        blockers.append(f"Employee Identity: {emp.get('reason') or emp_label}")
    if not real_on:
        blockers.append("REAL MODE NOT ENABLED — set REAL_INTEGRATION_ENABLED=true in .envs/.staging/.django")
    if fixture_isolation == STATUS_FAIL:
        blockers.append("Fixture modes must be false when REAL_INTEGRATION_ENABLED=true")

    if blockers:
        overall = "NOT READY"
        waiting = "WAITING FOR OPERATOR CONFIGURATION" if not real_on or channel_label != STATUS_READY else "NOT READY"
    else:
        # Config gate only — live probes still required for READY FOR REAL INTEGRATION
        overall = "CONFIG READY — LIVE PROBES REQUIRED"
        waiting = "CONFIGURED; run real_integration_activate_staging for live verification"

    return {
        "title": "REAL INTEGRATION STATUS",
        "environment": getattr(settings, "DEPLOYMENT_ENVIRONMENT", "UNKNOWN"),
        "settings_module": getattr(settings, "SETTINGS_MODULE", ""),
        "REAL_INTEGRATION_ENABLED": "TRUE" if real_on else "FALSE",
        "Channel-I": channel_label,
        "Channel-I_detail": channel_i,
        "Redirect": STATUS_VALID if redirect.get("status") == STATUS_VALID else STATUS_INVALID,
        "Redirect_detail": redirect,
        "Legacy_MySQL": mysql_label,
        "Legacy_MySQL_detail": mysql,
        "Employee_Identity": emp_label,
        "Employee_Identity_detail": emp,
        "S3": s3_label,
        "S3_detail": s3,
        "Fixture_Isolation": fixture_isolation,
        "Production": "NOT TOUCHED",
        "production_writes": "NO",
        "Overall": overall,
        "operator_message": waiting,
        "blockers": blockers,
        "never_edits_env": True,
    }


def format_status_human(report: dict) -> str:
    return "\n".join(
        [
            "REAL INTEGRATION STATUS",
            "",
            f"Environment: {report.get('environment')}",
            f"REAL_INTEGRATION_ENABLED: {report.get('REAL_INTEGRATION_ENABLED')}",
            "",
            f"Channel-I: {report.get('Channel-I')}",
            f"Redirect: {report.get('Redirect')}",
            f"Legacy MySQL: {report.get('Legacy_MySQL')}",
            f"Employee Identity: {report.get('Employee_Identity')}",
            f"S3: {report.get('S3')}",
            f"Fixture Isolation: {report.get('Fixture_Isolation')}",
            f"Production: {report.get('Production')}",
            f"Production Writes: {report.get('production_writes')}",
            "",
            f"Overall: {report.get('Overall')}",
            f"Note: {report.get('operator_message')}",
            *(["", "Blockers:"] + [f"  - {b}" for b in (report.get("blockers") or [])]),
        ]
    )
