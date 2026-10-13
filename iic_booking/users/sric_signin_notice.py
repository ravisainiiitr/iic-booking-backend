"""SRIC recharges for faculty who have not signed in to the new portal yet.

A recharge row whose Employee ID has no portal account (or whose account has never been signed in to) cannot be
credited. The faculty member is emailed, asking them to sign in with Channel i; once they have, the periodic recheck
matches the row to their account and credits it.

The address comes from the matched account, else from the old portal's records for that Employee ID: the legacy
wallet mapping first, then the old portal database (read-only).
"""

from __future__ import annotations

import logging
from decimal import Decimal
from html import escape
from typing import Iterable

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from iic_booking.users.models.sric_wallet_recharge import (
    SricWalletRecharge,
    SricWalletRechargeSettings,
    SricWalletRechargeStatus,
)
from iic_booking.users.sric_wallet_csv import normalize_employee_id

logger = logging.getLogger(__name__)

S = SricWalletRechargeStatus

SOURCE_PORTAL_ACCOUNT = "portal_account"
SOURCE_LEGACY_MAPPING = "legacy_wallet_mapping"
SOURCE_OLD_PORTAL = "old_portal"
SOURCE_ENTERED = "entered_by_admin"


def needs_signin_notice(rec: SricWalletRecharge) -> bool:
    """Waiting only because the faculty member has no portal account yet, or has never signed in to it."""
    if rec.is_test or rec.status != S.NEEDS_REVIEW:
        return False
    if rec.review_reason == "unmatched_employee":
        return True
    user = rec.matched_user if rec.matched_user_id else None
    return rec.review_reason == "inactive_user" and user is not None and user.last_login is None


def _valid(address: str) -> str:
    address = (address or "").strip()
    try:
        validate_email(address)
    except ValidationError:
        return ""
    return address


def _legacy_mapping_email(norm: str) -> str:
    from iic_booking.users.models.portal_migration import LegacyWalletAccountMapping

    for mapping in LegacyWalletAccountMapping.objects.filter(employee_id__iendswith=norm).only(
        "employee_id", "channel_i_email", "old_email"
    ):
        if normalize_employee_id(mapping.employee_id) != norm:
            continue
        for address in (mapping.channel_i_email, mapping.old_email):
            if _valid(address):
                return _valid(address)
    return ""


def _old_portal_email(norm: str) -> str:
    from iic_booking.users.legacy_ledger.reader import OldMySQLNotConfigured, OldMySQLReader

    try:
        with OldMySQLReader() as reader:
            rows = reader.fetchall(
                "SELECT emp_id, email FROM users WHERE TRIM(emp_id) LIKE %s ORDER BY id DESC LIMIT 20", (f"%{norm}",)
            )
    except OldMySQLNotConfigured:
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("SRIC sign-in reminder: old portal lookup failed (%s)", type(exc).__name__)
        return ""
    for row in rows:
        if normalize_employee_id(str(row.get("emp_id") or "")) == norm and _valid(str(row.get("email") or "")):
            return _valid(str(row.get("email")))
    return ""


def find_notice_address(rec: SricWalletRecharge) -> tuple[str, str]:
    """``(email, source)`` for the faculty member of ``rec``; ``("", "")`` when no address is known."""
    user = rec.matched_user if rec.matched_user_id else None
    if user is not None and _valid(user.email):
        return _valid(user.email), SOURCE_PORTAL_ACCOUNT
    norm = normalize_employee_id(rec.employee_id)
    if not norm:
        return "", ""
    address = _legacy_mapping_email(norm)
    if address:
        return address, SOURCE_LEGACY_MAPPING
    address = _old_portal_email(norm)
    if address:
        return address, SOURCE_OLD_PORTAL
    return "", ""


def _history(rec: SricWalletRecharge, action: str, actor=None, note: str = "") -> None:
    from iic_booking.users.sric_wallet_recharge import _history as add_history

    add_history(rec, action, actor, note)


def build_notice(rows: list[SricWalletRecharge]) -> tuple[str, str, str]:
    """``(subject, text, html)`` asking the faculty member of ``rows`` to sign in with Channel i."""
    from iic_booking.communication.email_branding import format_inr, wrap_email_html
    from iic_booking.communication.utils import get_frontend_absolute_url
    from iic_booking.users.sric_wallet_recharge import receiver_label

    first = rows[0]
    link = get_frontend_absolute_url("/login")
    name = (first.pi_name or "").strip() or "Sir / Madam"
    total = sum((r.amount for r in rows if r.amount is not None), Decimal("0.00"))
    amount = format_inr(total) or f"₹{total:,.2f}"
    plural = len(rows) > 1
    headline = "Sign in to the new IIC portal to receive your SRIC wallet recharge" + ("s" if plural else "")
    intro = (
        f"We have received {'your wallet recharges' if plural else 'your wallet recharge'} of {amount} from the SRIC "
        f"portal (rnd.iitr.ac.in) for Employee ID {first.employee_id}. "
        f"{'They' if plural else 'It'} cannot be credited yet because you have not signed in to the new IIC booking "
        "portal."
    )
    action = (
        "Please sign in to the new portal using Channel i (your IIT Roorkee Channel i account). After you sign in, "
        f"the recharge{'s are' if plural else ' is'} matched to your account and credited to your IIC wallet, and you "
        "will receive a confirmation email. You do not need to raise the recharge again on the SRIC portal."
    )
    details = [
        [
            ("Reference", r.reference),
            ("Amount", format_inr(r.amount) or r.amount_raw or "—"),
            ("SRIC Ledger ID", r.ledger_id),
            ("Project Number", r.project_number or "—"),
            ("Receiver", receiver_label(r)),
        ]
        for r in rows
    ]
    text = (
        f"Dear {name},\n\n{intro}\n\n{action}\n\nSign in to the new portal: {link}\n\n"
        + "\n\n".join("\n".join(f"{k}: {v}" for k, v in block) for block in details)
        + "\n"
    )
    tables = "".join(
        "<table cellpadding='0' cellspacing='0' style='font-family:Arial,Helvetica,sans-serif;font-size:14px;"
        "margin-bottom:12px;'>"
        + "".join(
            f"<tr><td style='padding:4px 12px 4px 0;color:#475569;'><strong>{escape(k)}</strong></td>"
            f"<td style='padding:4px 0;color:#0f172a;'>{escape(str(v))}</td></tr>"
            for k, v in block
        )
        + "</table>"
        for block in details
    )
    body = (
        f"<p>Dear {escape(name)},</p><p>{escape(intro)}</p><p>{escape(action)}</p>"
        f"<p style='margin:20px 0;'><a href='{escape(link)}' style='background:#1d4ed8;color:#ffffff;padding:10px 18px;"
        f"border-radius:6px;text-decoration:none;font-weight:bold;'>Sign in with Channel i</a></p>"
        f"<p style='font-size:13px;color:#475569;'>Or open this link: <a href='{escape(link)}'>{escape(link)}</a></p>"
        f"{tables}"
    )
    references = ", ".join(r.reference for r in rows)
    subject = f"[{references}] Action needed: {headline}"
    html = wrap_email_html(title=headline, subtitle=references, body_inner_html=body)
    return subject, text, html


def _deliver(rows: list[SricWalletRecharge], address: str, source: str, *, actor=None) -> None:
    from iic_booking.communication.styled_transactional_emails import _send

    subject, text, html = build_notice(rows)
    _send(address, subject, text, html)
    now = timezone.now()
    with transaction.atomic():
        for locked in SricWalletRecharge.objects.select_for_update().filter(pk__in=[r.pk for r in rows]):
            locked.signin_notice_sent_at = now
            locked.signin_notice_email = address[:255]
            _history(locked, "signin_notice_sent", actor, f"to {address} ({source})")
            locked.save(update_fields=["signin_notice_sent_at", "signin_notice_email", "history", "updated_at"])


def send_new_row_notices(row_ids: Iterable[int]) -> dict[str, int]:
    """Email each faculty member once for newly stored rows waiting on their sign-in (one email per address)."""
    out = {"sent": 0, "rows": 0, "no_address": 0}
    if not SricWalletRechargeSettings.get_singleton().signin_notice_enabled:
        return out
    rows = [
        r
        for r in SricWalletRecharge.objects.select_related("matched_user", "receiver_mapping")
        .filter(pk__in=list(row_ids), signin_notice_sent_at__isnull=True)
        .order_by("pk")
        if needs_signin_notice(r)
    ]
    groups: dict[str, tuple[str, str, list[SricWalletRecharge]]] = {}
    resolved: dict[str, tuple[str, str]] = {}
    for rec in rows:
        key = normalize_employee_id(rec.employee_id) or f"row-{rec.pk}"
        if key not in resolved:
            resolved[key] = find_notice_address(rec)
        address, source = resolved[key]
        if not address:
            out["no_address"] += 1
            continue
        groups.setdefault(address.lower(), (address, source, []))[2].append(rec)
    for address, source, group in groups.values():
        try:
            _deliver(group, address, source)
        except Exception:  # noqa: BLE001
            logger.exception("SRIC sign-in reminder failed for rows %s", [r.pk for r in group])
            continue
        out["sent"] += 1
        out["rows"] += len(group)
    if out["sent"] or out["no_address"]:
        logger.info("SRIC sign-in reminders: %s", out)
    return out


def send_row_notice(row_id: int, *, actor, email: str = "") -> SricWalletRecharge:
    """Main Administrator action: email (again) the faculty member of one row, optionally to an address they enter."""
    from iic_booking.users.sric_wallet_recharge import SricRechargeError

    rec = SricWalletRecharge.objects.select_related("matched_user", "receiver_mapping").get(pk=row_id)
    if not needs_signin_notice(rec):
        raise SricRechargeError(
            "Only rows waiting for a faculty member who has not signed in yet can be sent this email.",
            status=409,
            code="NOT_WAITING_FOR_SIGNIN",
        )
    if email:
        address, source = _valid(email), SOURCE_ENTERED
        if not address:
            raise SricRechargeError("Enter a valid email address.", code="INVALID_EMAIL")
    else:
        address, source = find_notice_address(rec)
        if not address:
            raise SricRechargeError(
                "No email address is known for this Employee ID. Enter the faculty member's email address.",
                code="EMAIL_REQUIRED",
            )
    try:
        _deliver([rec], address, source, actor=actor)
    except Exception as exc:  # noqa: BLE001
        logger.exception("SRIC sign-in reminder failed for row %s", rec.pk)
        raise SricRechargeError("The email could not be sent. Please try again.", status=502, code="SEND_FAILED") from exc
    rec.refresh_from_db()
    return rec
