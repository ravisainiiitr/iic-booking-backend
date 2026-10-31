"""Versioned policy wording that users acknowledge, and the Main Administrator's settings for the services.

Wording is data, not code: a new version is a new append-only row, and every acknowledgement stores the version and
a SHA-256 of the exact text shown. A client cannot acknowledge a version that is not the current one.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from django.db import transaction

from .models import ExternalServicesSettings, ExternalServicesSettingsChange, PolicyText

BOOLEAN_SETTINGS = (
    "special_requests_enabled",
    "special_request_surcharge",
    "invoice_requests_enabled",
    "early_invoice_allowed",
    "auto_request_without_preference",
    "physical_copies_enabled",
    "sample_labels_enabled",
    "govt_credit_enabled",
    "cancellation_policy_enabled",
)
SETTINGS_FIELDS = BOOLEAN_SETTINGS + (
    "max_open_special_requests",
    "short_notice_days",
    "payment_window_hours",
    "sric_dispatch_mode",
    "sric_recipient_email",
    "sric_test_recipient_email",
    "iic_postal_address",
    "head_iic_emails",
    "cancellation_free_hours",
    "cancellation_half_hours",
    "cancellation_half_penalty_percent",
)


class PolicyError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", field: str = "", extra=None):
        super().__init__(message)
        self.message, self.status, self.code, self.field = message, status, code, field
        self.extra = extra or {}

    def payload(self) -> dict:
        body = {"error": self.message, "code": self.code}
        if self.field:
            body["field"] = self.field
        body.update(self.extra)
        return body


def text_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def current(key: str) -> Optional[PolicyText]:
    return PolicyText.objects.filter(key=key).order_by("-version").first()


def policy_dict(policy: Optional[PolicyText]) -> Optional[dict]:
    if policy is None:
        return None
    return {
        "key": policy.key,
        "version": policy.version,
        "text": policy.text,
        "approved": policy.approved,
        "approval_note": policy.approval_note,
        "sha256": text_sha256(policy.text),
    }


def require_acknowledgement(key: str, version) -> PolicyText:
    """The current policy row, provided ``version`` is exactly its version (the text the user was shown)."""
    policy = current(key)
    if policy is None:
        raise PolicyError("The wording for this acknowledgement is not set up yet.", code="policy_missing")
    try:
        given = int(version)
    except (TypeError, ValueError):
        given = None
    if given != policy.version:
        raise PolicyError(
            "Please read and accept the current conditions.",
            code="acknowledgement_required",
            field=f"{key}_ack_version",
            extra={"policy": policy_dict(policy)},
        )
    return policy


def add_version(actor, key: str, text: str, *, approved: bool = False, approval_note: str = "") -> PolicyText:
    from .window_admin import require_main_admin

    require_main_admin(actor)
    text = (text or "").strip()
    if len(text) < 20:
        raise PolicyError("The wording must be at least 20 characters.", field="text")
    with transaction.atomic():
        last = PolicyText.objects.select_for_update().filter(key=key).order_by("-version").first()
        return PolicyText.objects.create(
            key=key, version=(last.version + 1) if last else 1, text=text, approved=bool(approved),
            approval_note=(approval_note or "")[:255], created_by=actor,
        )


def _email(value, field) -> str:
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        validate_email(text)
    except ValidationError:
        raise PolicyError(f"{field} must be an email address.", field=field)
    return text


def settings_dict(s: ExternalServicesSettings) -> dict:
    return {f: getattr(s, f) for f in SETTINGS_FIELDS}


def update_settings(actor, data: dict) -> ExternalServicesSettings:
    from .window_admin import _bool, _int, _reason, require_main_admin

    require_main_admin(actor)
    reason = _reason(data.get("reason"))
    unknown = sorted(set(data) - set(SETTINGS_FIELDS) - {"reason"})
    if unknown:
        raise PolicyError(f"These settings cannot be changed here: {', '.join(unknown)}.", field=unknown[0])
    with transaction.atomic():
        s = ExternalServicesSettings.objects.select_for_update().get(pk=ExternalServicesSettings.load().pk)
        before = settings_dict(s)
        for field in BOOLEAN_SETTINGS:
            if field in data:
                setattr(s, field, _bool(data[field], field))
        limits = {"max_open_special_requests": (1, 20), "short_notice_days": (1, 60), "payment_window_hours": (1, 168),
                  "cancellation_free_hours": (1, 720), "cancellation_half_hours": (0, 720),
                  "cancellation_half_penalty_percent": (0, 100)}
        for field, (lo, hi) in limits.items():
            if field in data:
                setattr(s, field, _int(data[field], field, lo, hi))
        if "sric_dispatch_mode" in data:
            mode = str(data["sric_dispatch_mode"] or "").strip().lower()
            if mode not in ("off", "test", "live"):
                raise PolicyError("sric_dispatch_mode must be off, test or live.", field="sric_dispatch_mode")
            s.sric_dispatch_mode = mode
        for field in ("sric_recipient_email", "sric_test_recipient_email"):
            if field in data:
                setattr(s, field, _email(data[field], field))
        if "iic_postal_address" in data:
            lines = [ln.strip() for ln in str(data["iic_postal_address"] or "").splitlines() if ln.strip()]
            if len(lines) > 8 or any(len(ln) > 80 for ln in lines):
                raise PolicyError("Use at most 8 lines of up to 80 characters.", field="iic_postal_address")
            s.iic_postal_address = "\n".join(lines)
        if "head_iic_emails" in data:
            emails = [ln.strip() for ln in str(data["head_iic_emails"] or "").replace(",", "\n").splitlines() if ln.strip()]
            if len(emails) > 5:
                raise PolicyError("Give at most 5 Head IIC addresses.", field="head_iic_emails")
            s.head_iic_emails = "\n".join(_email(e, "head_iic_emails") for e in emails)
        if s.cancellation_half_hours >= s.cancellation_free_hours:
            raise PolicyError("The full-penalty limit must be fewer hours than the free-cancellation limit.",
                              field="cancellation_half_hours")
        if s.govt_credit_enabled and not s.head_iic_emails:
            raise PolicyError("Set the Head IIC addresses before enabling credit requests.", field="head_iic_emails")
        if s.sric_dispatch_mode == "live" and not s.sric_recipient_email:
            raise PolicyError("Set the SRIC recipient address before live sending.", field="sric_recipient_email")
        if s.sric_dispatch_mode == "test" and not s.sric_test_recipient_email:
            raise PolicyError("Set the test address before test sending.", field="sric_test_recipient_email")
        if s.early_invoice_allowed and not PolicyText.objects.filter(key="early_invoice").exists():
            raise PolicyError("Add the early-invoice disclosure wording first.", field="early_invoice_allowed")
        s.updated_by = actor
        s.save()
        ExternalServicesSettingsChange.objects.create(actor=actor, before=before, after=settings_dict(s), reason=reason)
    return s
