"""Live REAL staging evidence: Channel-I identity + legacy emp_id match.

Re-verifies durable facts left by a completed Omniport callback (ChannelIIdentityProfile)
against live read-only MySQL. Does NOT exchange OAuth codes or print tokens/secrets/PII.

Never fabricates OAuth. Never treats CONFIGURED-only as PASS.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from iic_booking.users.legacy_ledger.real_integration_guards import (
    ALLOWED_EMPLOYEE_ID_CLAIMS,
    EXPECTED_OMNIPORT_CALLBACK_PATH,
    STATUS_BLOCKED,
    STATUS_CONFIGURED,
    STATUS_FAIL,
    STATUS_NOT_TESTED,
    STATUS_PASS,
    channel_i_preflight_status,
    employee_id_claim_status,
    omniport_redirect_uri_status,
    real_integration_enabled,
)


LIVE_EVIDENCE_FILENAME = "real_channel_i_live_evidence.json"


def live_evidence_path() -> Path:
    return Path(settings.BASE_DIR) / "docs" / "release" / "migration" / LIVE_EVIDENCE_FILENAME


def probe_live_channel_i_identity() -> dict[str, Any]:
    """Prove live Channel-I identity qualification without a new token exchange.

    PASS requires ALL of:
      - fixture modes off
      - Omniport credentials + valid redirect (config REAL)
      - authoritative claim configured and recognized
      - durable ChannelIIdentityProfile with the claim field populated
        (created by a prior real Omniport callback — not fixtures)
      - OldMySQLReader exact match of claim value to admin.users.emp_id count == 1
    """
    redirect = omniport_redirect_uri_status()
    channel_cfg = channel_i_preflight_status()
    emp_cfg = employee_id_claim_status()

    fixture_ci = bool(getattr(settings, "CHANNEL_I_STAGING_FIXTURE_MODE", False))
    fixture_my = bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False))
    if fixture_ci or fixture_my:
        return {
            "status": STATUS_BLOCKED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": STATUS_FAIL,
            "live_userinfo": STATUS_FAIL,
            "redirect": redirect.get("status"),
            "reason": "Fixture modes must be false for REAL Channel-I evidence",
            "fixture_fallback": "BLOCKED",
            "claim_pass": False,
            "employee_identity_pass": False,
        }

    if redirect.get("status") != "VALID":
        return {
            "status": STATUS_BLOCKED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": STATUS_NOT_TESTED,
            "live_userinfo": STATUS_NOT_TESTED,
            "redirect": "FAIL",
            "reason": "CHANNEL-I REDIRECT URI INVALID",
            "claim_pass": False,
            "employee_identity_pass": False,
        }

    if channel_cfg.get("status") not in (STATUS_CONFIGURED, STATUS_PASS):
        return {
            "status": STATUS_BLOCKED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": STATUS_NOT_TESTED,
            "live_userinfo": STATUS_NOT_TESTED,
            "redirect": STATUS_PASS,
            "reason": channel_cfg.get("reason") or "Channel-I not configured",
            "claim_pass": False,
            "employee_identity_pass": False,
        }

    claim = (emp_cfg.get("claim") or "").strip()
    if emp_cfg.get("status") == STATUS_BLOCKED or not claim:
        return {
            "status": STATUS_BLOCKED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": STATUS_NOT_TESTED,
            "live_userinfo": STATUS_NOT_TESTED,
            "redirect": STATUS_PASS,
            "reason": emp_cfg.get("reason") or "Employee claim not configured",
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
        }

    if claim not in ALLOWED_EMPLOYEE_ID_CLAIMS:
        return {
            "status": STATUS_BLOCKED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": STATUS_NOT_TESTED,
            "live_userinfo": STATUS_NOT_TESTED,
            "redirect": STATUS_PASS,
            "reason": "Unrecognized employee claim",
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
        }

    # Durable identity from prior real Omniport callback (no tokens / no PII logged).
    try:
        from iic_booking.users.models.channel_i_identity import ChannelIIdentityProfile

        profile = (
            ChannelIIdentityProfile.objects.exclude(channel_i_username="")
            .exclude(channel_i_username__isnull=True)
            .order_by("-last_channel_i_sync", "-id")
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_BLOCKED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": "OPERATOR ACTION REQUIRED",
            "live_userinfo": STATUS_NOT_TESTED,
            "redirect": STATUS_PASS,
            "reason": f"Cannot load ChannelIIdentityProfile: {type(exc).__name__}",
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
        }

    if profile is None:
        return {
            "status": STATUS_CONFIGURED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": "OPERATOR ACTION REQUIRED",
            "live_userinfo": STATUS_NOT_TESTED,
            "redirect": STATUS_PASS,
            "reason": (
                "No durable Channel-I identity profile with username — "
                "complete one staging Omniport login first"
            ),
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
            "fixture_fallback": "NONE",
        }

    # Extract claim value without treating email/name as identity.
    username = (profile.channel_i_username or "").strip()
    enrolment = (profile.student_enrolment_number or "").strip()
    claim_value = ""
    claim_source = ""
    if claim == "username":
        claim_value = username
        claim_source = "username"
    elif claim in {"student.enrolmentNumber", "student.enrolment_number"}:
        claim_value = enrolment
        claim_source = "student.enrolmentNumber"
    elif claim == "facultyMember.employeeId":
        # Not stored as a dedicated column; cannot prove without raw payload.
        return {
            "status": STATUS_CONFIGURED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": "OPERATOR ACTION REQUIRED",
            "live_userinfo": STATUS_NOT_TESTED,
            "redirect": STATUS_PASS,
            "reason": "facultyMember.employeeId not available on durable profile columns",
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
        }
    elif claim == "operator_confirmed_map":
        if enrolment:
            claim_value = enrolment
            claim_source = "student.enrolmentNumber"
        elif username:
            claim_value = username
            claim_source = "username"
        else:
            claim_value = ""
            claim_source = ""

    if not claim_value:
        return {
            "status": STATUS_CONFIGURED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": "OPERATOR ACTION REQUIRED",
            "live_userinfo": STATUS_FAIL,
            "redirect": STATUS_PASS,
            "reason": f"Durable profile missing value for claim {claim}",
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
            "has_username": bool(username),
            "has_enrolment": bool(enrolment),
        }

    # Live RO MySQL match — never print claim_value.
    try:
        from iic_booking.users.legacy_ledger.reader import OldMySQLReader
        from iic_booking.users.legacy_ledger.snapshot_reader import get_legacy_reader

        reader = get_legacy_reader(require_real=True)
    except ImproperlyConfigured as exc:
        return {
            "status": STATUS_BLOCKED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": STATUS_PASS if profile else STATUS_NOT_TESTED,
            "live_userinfo": STATUS_PASS if username else STATUS_NOT_TESTED,
            "redirect": STATUS_PASS,
            "reason": str(exc),
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
            "legacy_match": STATUS_FAIL,
        }

    if not isinstance(reader, OldMySQLReader) and not getattr(
        reader, "_real_integration_old_mysql_reader", False
    ):
        return {
            "status": STATUS_FAIL,
            "evidence_class": "FIXTURE",
            "live_oauth": STATUS_FAIL,
            "live_userinfo": STATUS_FAIL,
            "redirect": STATUS_PASS,
            "reason": "REAL identity check received non-OldMySQLReader — fixture isolation failure",
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
            "fixture_fallback": "DETECTED",
            "legacy_match": STATUS_FAIL,
        }

    try:
        with reader:
            rows = reader.fetchall(
                "SELECT emp_id FROM users WHERE emp_id = %s LIMIT 5",
                (claim_value,),
            )
            match_count = len(rows or [])
    except Exception as exc:  # noqa: BLE001
        return {
            "status": STATUS_BLOCKED,
            "evidence_class": "NOT_TESTED",
            "live_oauth": STATUS_PASS,
            "live_userinfo": STATUS_PASS,
            "redirect": STATUS_PASS,
            "reason": f"MySQL emp_id probe failed: {type(exc).__name__}",
            "claim": claim,
            "claim_pass": False,
            "employee_identity_pass": False,
            "legacy_match": STATUS_FAIL,
            "fixture_fallback": "NONE",
        }

    if match_count != 1:
        return {
            "status": STATUS_FAIL,
            "evidence_class": "REAL",
            "live_oauth": STATUS_PASS,
            "live_userinfo": STATUS_PASS,
            "redirect": STATUS_PASS,
            "reason": f"emp_id exact match count={match_count} (required 1)",
            "claim": claim,
            "claim_source": claim_source,
            "claim_pass": False,
            "employee_identity_pass": False,
            "legacy_match": STATUS_FAIL,
            "exact_match_count": match_count,
            "wallet_identity": STATUS_FAIL,
            "fixture_fallback": "NONE",
            "email_name_fallback": False,
            "profile_has_sync": bool(getattr(profile, "last_channel_i_sync", None)),
            "has_student_payload": bool(getattr(profile, "has_student_payload", False)),
            "has_faculty_payload": bool(getattr(profile, "has_faculty_payload", False)),
        }

    return {
        "status": STATUS_PASS,
        "evidence_class": "REAL",
        "live_oauth": STATUS_PASS,
        "live_userinfo": STATUS_PASS,
        "redirect": STATUS_PASS,
        "reason": (
            "Prior real Omniport callback left durable identity; "
            "re-verified claim against admin.users.emp_id (exact match=1); "
            "no new token exchange; no fixture fallback"
        ),
        "claim": claim,
        "claim_source": claim_source,
        "claim_pass": True,
        "employee_identity_pass": True,
        "legacy_match": STATUS_PASS,
        "exact_match_count": 1,
        "wallet_identity": STATUS_PASS,
        "fixture_fallback": "NONE",
        "email_name_fallback": False,
        "profile_has_sync": bool(getattr(profile, "last_channel_i_sync", None)),
        "has_student_payload": bool(getattr(profile, "has_student_payload", False)),
        "has_faculty_payload": bool(getattr(profile, "has_faculty_payload", False)),
        "expected_callback": EXPECTED_OMNIPORT_CALLBACK_PATH,
        "real_integration_enabled": real_integration_enabled(),
        "verification_method": "durable_identity_reverification",
    }


def employee_identity_from_live_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """Map live Channel-I probe into Employee_Identity preflight/activation shape."""
    base = employee_id_claim_status()
    if probe.get("employee_identity_pass") and probe.get("claim_pass"):
        return {
            "status": STATUS_PASS,
            "claim": probe.get("claim") or base.get("claim"),
            "claim_source": probe.get("claim_source"),
            "reason": "Live claim verified against admin.users.emp_id (exact match=1)",
            "wallet_identity": STATUS_PASS,
            "legacy_match": STATUS_PASS,
            "claim_pass": True,
            "recognized": True,
            "evidence_class": "REAL",
            "exact_match_count": probe.get("exact_match_count"),
            "email_name_fallback": False,
        }
    if probe.get("live_oauth") == "OPERATOR ACTION REQUIRED":
        return {
            **base,
            "status": STATUS_CONFIGURED,
            "wallet_identity": STATUS_NOT_TESTED,
            "claim_pass": False,
            "evidence_class": "NOT_TESTED",
            "reason": probe.get("reason") or base.get("reason"),
        }
    return {
        **base,
        "status": probe.get("status") if probe.get("status") in (STATUS_BLOCKED, STATUS_FAIL) else base.get("status"),
        "wallet_identity": probe.get("wallet_identity") or STATUS_NOT_TESTED,
        "legacy_match": probe.get("legacy_match") or STATUS_NOT_TESTED,
        "claim_pass": False,
        "evidence_class": probe.get("evidence_class") or "NOT_TESTED",
        "reason": probe.get("reason") or base.get("reason"),
    }


def channel_i_from_live_probe(probe: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge live probe into Channel-I status block."""
    cfg = config or channel_i_preflight_status()
    if probe.get("status") == STATUS_PASS and probe.get("evidence_class") == "REAL":
        return {
            **cfg,
            "status": STATUS_PASS,
            "evidence_class": "REAL",
            "live_oauth": STATUS_PASS,
            "live_userinfo": STATUS_PASS,
            "redirect": STATUS_PASS,
            "claim_pass": True,
            "reason": probe.get("reason"),
            "verification_method": probe.get("verification_method"),
            "fixture_fallback": "NONE",
        }
    merged = {
        **cfg,
        "live_oauth": probe.get("live_oauth", STATUS_NOT_TESTED),
        "live_userinfo": probe.get("live_userinfo", STATUS_NOT_TESTED),
        "redirect": probe.get("redirect", cfg.get("redirect_uri_matches_expected_callback")),
        "evidence_class": probe.get("evidence_class") or cfg.get("evidence_class") or "NOT_TESTED",
        "claim_pass": bool(probe.get("claim_pass")),
    }
    if probe.get("reason") and probe.get("status") != STATUS_PASS:
        merged["reason"] = probe.get("reason")
    if probe.get("status") in (STATUS_BLOCKED, STATUS_FAIL):
        merged["status"] = probe.get("status")
    return merged


def write_live_channel_i_evidence(probe: dict[str, Any]) -> str:
    """Persist redacted live Channel-I evidence JSON. Never writes secrets or claim values."""
    path = live_evidence_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "title": "REAL Channel-I live evidence",
        "evidence_class": probe.get("evidence_class"),
        "status": probe.get("status"),
        "live_oauth": probe.get("live_oauth"),
        "live_userinfo": probe.get("live_userinfo"),
        "redirect": probe.get("redirect"),
        "claim": probe.get("claim"),
        "claim_source": probe.get("claim_source"),
        "claim_pass": probe.get("claim_pass"),
        "legacy_match": probe.get("legacy_match"),
        "exact_match_count": probe.get("exact_match_count"),
        "wallet_identity": probe.get("wallet_identity"),
        "fixture_fallback": probe.get("fixture_fallback"),
        "email_name_fallback": probe.get("email_name_fallback", False),
        "verification_method": probe.get("verification_method"),
        "reason": probe.get("reason"),
        "production_writes": "NO",
        "secrets_included": False,
        "claim_value_included": False,
    }
    path.write_text(json.dumps(safe, indent=2) + "\n", encoding="utf-8")
    return str(path)
