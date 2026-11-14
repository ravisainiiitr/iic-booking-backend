"""Refunds to a bank account: the user's confirmation, the request to SRIC, and the settlement.

Flow (14 Nov 2026):

1. A refund to a bank account is recorded (``refunds.cancel`` or ``refunds.refund_lab_failure``). The amount follows
   the user's entitlement: the full amount (service charges + GST), or the base amount only when a GST invoice has
   already been requested or issued (SRIC settles the tax with the invoice).
2. The user reviews the account details and confirms them (``account_confirmed_at``). A refund the lab recorded
   without an account waits as AWAITING_ACCOUNT until the user gives and confirms one (``confirm_account``).
3. Once confirmed it is QUEUED and visible to the Accounts In Charge and the Main Administrator, and the request goes
   to SRIC by email: at once, or with the weekly batch (``ExternalServicesSettings.refund_dispatch_timing``). The
   off / test / live switch of the SRIC dispatch applies; nothing is sent while it is off.
4. Each refund is one email built from the Main Administrator's template, with the full details: user, original
   payment, amounts and deductions, equipment, host department and the bank account. What was sent is stored on the
   refund (``sric_subject`` / ``sric_body``) for the record.
5. The Accounts In Charge or the Main Administrator record the settlement (amount, date, mode, bank reference). The
   user is then told (portal, email and WhatsApp) the amount paid and every deduction, including GST kept because an
   invoice was issued.

Sending is claimed by compare-and-swap (QUEUED/FAILED -> SENDING) and done outside any transaction. A send that a
crashed worker left mid-way becomes UNCERTAIN and is never resent automatically: a duplicate request could mean a
duplicate payment, so staff check with SRIC and resend explicitly.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

from .models import (
    ExternalRefund,
    ExternalServicesSettings,
    RefundBankAccount,
    RefundDestination,
    RefundStatus,
    SettlementMode,
)
from .refunds import RefundPolicyError

logger = logging.getLogger(__name__)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_ATTEMPTS = 5
STALE_SENDING = timedelta(minutes=30)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

DEFAULT_SUBJECT = "Refund to bank account: {booking_reference} — {user_name} — Rs. {refund_amount}"
DEFAULT_BODY = """Dear SRIC team,

Please transfer the refund below to the bank account given and confirmed by the user. The amount was received through the IIC Equipment Booking Portal and is refunded under the IIC refund policy ({entitlement}).

{details}

Once the transfer is made, please share the bank reference (UTR) and the date with the IIC Accounts In Charge, who will record the settlement on the portal.

Regards,
IIC Equipment Booking Portal
Institute Instrumentation Centre, IIT Roorkee"""

PLACEHOLDERS = {
    "booking_reference": "Booking reference",
    "fbr_number": "I-STEM FBR number (if any)",
    "user_name": "User's name",
    "user_email": "User's email",
    "user_phone": "User's mobile number",
    "organisation": "User's organisation (billing name)",
    "equipment": "Equipment name",
    "equipment_code": "Equipment code",
    "department": "Host department",
    "paid_amount": "Amount originally paid (GST included)",
    "refund_amount": "Amount to transfer",
    "refund_base": "Refund before GST",
    "refund_gst": "GST refunded",
    "deductions_total": "Total deducted",
    "entitlement": "Full refund, or base amount only",
    "reason": "Why the refund is made",
    "account_holder": "Account holder's name",
    "bank_name": "Bank",
    "account_number": "Account number",
    "ifsc": "IFSC",
    "branch": "Branch",
    "details": "The complete details block (always added if missing)",
}


BankRefundError = RefundPolicyError  # one error type, so every refund endpoint reports it the same way


def _m(value) -> Decimal:
    return Decimal(str(value or "0")).quantize(CENT, rounding=ROUND_HALF_UP)


def _rs(value) -> str:
    return f"{_m(value):,.2f}"


def _addresses(text: str) -> list[str]:
    out = []
    for part in re.split(r"[\s,;]+", str(text or "")):
        part = part.strip()
        if part and part.lower() not in {a.lower() for a in out}:
            out.append(part)
    return out


def validate_addresses(text: str, label: str) -> str:
    bad = [a for a in _addresses(text) if not _EMAIL.match(a)]
    if bad:
        raise BankRefundError(f"{label}: '{bad[0]}' is not an email address.", code="invalid_email")
    return "\n".join(_addresses(text))


def recipients(s: ExternalServicesSettings) -> tuple[list[str], list[str]]:
    """(to, cc) for the current dispatch mode; ([], []) while off or unconfigured."""
    if s.sric_dispatch_mode == "live":
        to = _addresses(s.refund_sric_recipients) or _addresses(s.sric_recipient_email)
        return to, [a for a in _addresses(s.refund_sric_cc) if a not in to]
    if s.sric_dispatch_mode == "test":
        return _addresses(s.refund_sric_test_recipients) or _addresses(s.sric_test_recipient_email), []
    return [], []


# --- the figures -------------------------------------------------------------------------------------------------------

def deductions(r: ExternalRefund) -> list[dict]:
    """What was kept from the amount paid, line by line; the lines add up to ``paid - refund``."""
    paid, gst = _m(r.paid_amount), _m(r.gst_paid)
    taxable = paid - gst
    rate = (gst / taxable) if taxable > 0 else ZERO
    ship_base = _m(_m(r.return_shipping) / (1 + rate)) if rate else _m(r.return_shipping)
    ship_back_base = _m(_m(r.shipping_refunded) / (1 + rate)) if (rate and r.gst_refunded) else _m(r.shipping_refunded)
    service_base = max(ZERO, taxable - ship_base)
    service_back = max(ZERO, _m(r.refund_base) - ship_back_base)
    out = []
    service_kept = max(ZERO, service_base - service_back)
    if service_kept > 0:
        if r.penalty_percent:
            label = f"Cancellation penalty ({r.penalty_percent}% of the service charges)"
        elif r.portion_percent < 100:
            label = f"Service charges not refunded (refund of {r.portion_percent}% of the service charges)"
        else:
            label = "Service charges kept"
        out.append({"code": "service", "label": label, "amount": str(service_kept)})
    ship_kept = max(ZERO, ship_base - ship_back_base)
    if ship_kept > 0:
        out.append({"code": "shipping", "label": "Return shipping charge (non-refundable)", "amount": str(ship_kept)})
    total_kept = max(ZERO, paid - _m(r.refund_amount))
    gst_kept = total_kept - sum((_m(d["amount"]) for d in out), ZERO)
    if gst_kept > 0:
        label = ("GST not refunded: a GST invoice was issued for this booking, so SRIC settles the tax with the invoice"
                 if not r.gst_refunded else "GST on the amounts kept")
        out.append({"code": "gst", "label": label, "amount": str(gst_kept)})
    return out


def entitlement(r: ExternalRefund) -> dict:
    if r.gst_refunded:
        return {"code": "full", "label": "Full refund: service charges with GST"}
    return {"code": "base_only", "label": "Base amount only: GST not refunded because a GST invoice was issued"}


def _payments(booking, paid) -> list[dict]:
    """How the booking was paid: online (Razorpay, with bank and settlement references), bank transfer receipts, and
    whatever the user's IIC wallet covered."""
    from .invoices import _payments as invoice_payments
    from .sric_dispatch import _payment_line

    rows = []
    for p in invoice_payments(booking):
        rows.append({**p, "line": _payment_line(p).strip().lstrip("- ").strip()})
    outside = sum((_m(p.get("amount")) for p in rows), ZERO)
    from_wallet = _m(paid) - outside
    if from_wallet > 0:
        rows.append({"method": "IIC wallet", "reference": "Paid from the user's IIC wallet", "amount": str(from_wallet),
                     "line": f"IIC wallet: Rs. {_rs(from_wallet)} paid from the user's IIC wallet on the portal"})
    return rows


def context(r: ExternalRefund) -> dict:
    """Everything the SRIC request and the staff detail page show about one refund."""
    from iic_booking.communication.utils import booking_display_id_for_email
    from iic_booking.users.display import get_user_display_name

    from .invoices import _billing

    booking, user = r.booking, r.user
    equipment = booking.equipment
    dept = getattr(equipment, "internal_department", None)
    bank = r.bank_snapshot or {}
    try:
        billing = _billing(booking)
    except Exception:  # noqa: BLE001 - billing is informative here, never a reason not to refund
        billing = {}
    starts = r.booking_starts_at or booking.daily_slots.order_by("start_datetime").values_list(
        "start_datetime", flat=True).first()
    ded = deductions(r)
    ent = entitlement(r)
    return {
        "booking_reference": booking_display_id_for_email(booking),
        "fbr_number": (getattr(booking, "istem_fbr_number", "") or "").strip(),
        "booking_date": timezone.localtime(starts).strftime("%d %b %Y, %H:%M") if starts else "",
        "booking_status": booking.get_status_display() if hasattr(booking, "get_status_display") else booking.status,
        "user_name": get_user_display_name(user) or user.email,
        "user_email": user.email,
        "user_phone": (getattr(user, "phone_number", "") or "").strip(),
        "organisation": (billing.get("billing_name") or "").strip(),
        "gstin": (billing.get("gstin") or "").strip(),
        "equipment": getattr(equipment, "name", ""),
        "equipment_code": getattr(equipment, "code", "") or "",
        "department": getattr(dept, "name", "") or "",
        "paid_amount": _rs(r.paid_amount),
        "gst_paid": _rs(r.gst_paid),
        "refund_base": _rs(r.refund_base),
        "refund_gst": _rs(r.refund_gst),
        "refund_amount": _rs(r.refund_amount),
        "deductions": ded,
        "deductions_total": _rs(sum((_m(d["amount"]) for d in ded), ZERO)),
        "entitlement": ent["label"],
        "entitlement_code": ent["code"],
        "reason": r.reason or r.get_origin_display(),
        "payments": _payments(booking, r.paid_amount),
        "account_holder": bank.get("account_holder_name", ""),
        "bank_name": bank.get("bank_name", ""),
        "account_number": bank.get("account_number", ""),
        "ifsc": bank.get("ifsc", ""),
        "branch": bank.get("branch", ""),
        "account_confirmed_at": (timezone.localtime(r.account_confirmed_at).strftime("%d %b %Y, %H:%M")
                                 if r.account_confirmed_at else ""),
        "refund_id": r.pk,
        "recorded_at": timezone.localtime(r.created_at).strftime("%d %b %Y, %H:%M") if r.created_at else "",
    }


def _sections(c: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    pay_rows = [(p.get("method", "Payment"), p.get("line", "")) for p in c["payments"]]
    refund_rows = [("Amount originally paid (GST included)", f"Rs. {c['paid_amount']}"),
                   ("GST in the amount paid", f"Rs. {c['gst_paid']}")]
    refund_rows += [(f"Less: {d['label']}", f"Rs. {_rs(d['amount'])}") for d in c["deductions"]]
    refund_rows += [("Refund before GST", f"Rs. {c['refund_base']}"), ("GST refunded", f"Rs. {c['refund_gst']}"),
                    ("AMOUNT TO TRANSFER", f"Rs. {c['refund_amount']}"), ("Entitlement", c["entitlement"]),
                    ("Reason", c["reason"])]
    return [
        ("Refund", refund_rows),
        ("Bank account (confirmed by the user" + (f" on {c['account_confirmed_at']})" if c["account_confirmed_at"] else ")"), [
            ("Account holder", c["account_holder"]), ("Bank", c["bank_name"]), ("Branch", c["branch"] or "-"),
            ("Account number", c["account_number"]), ("IFSC", c["ifsc"]),
        ]),
        ("User", [("Name", c["user_name"]), ("Email", c["user_email"]), ("Mobile", c["user_phone"] or "-"),
                  ("Organisation", c["organisation"] or "-"), ("GSTIN", c["gstin"] or "-")]),
        ("Booking and equipment", [
            ("Booking reference", c["booking_reference"]), ("I-STEM FBR", c["fbr_number"] or "-"),
            ("Booking date", c["booking_date"] or "-"), ("Equipment", f"{c['equipment']} ({c['equipment_code']})".replace(" ()", "")),
            ("Host department", c["department"] or "-"), ("Portal refund number", str(c["refund_id"])),
        ]),
        ("Original payment", pay_rows),
    ]


def details_text(c: dict) -> str:
    lines = []
    for title, rows in _sections(c):
        lines += [title.upper(), *(f"  {k}: {v}" for k, v in rows), ""]
    return "\n".join(lines).rstrip()


def details_html(c: dict) -> str:
    th = "style='text-align:left;padding:4px 10px 4px 0;color:#555;font-weight:normal;vertical-align:top'"
    td = "style='padding:4px 0;vertical-align:top'"
    parts = []
    for title, rows in _sections(c):
        body = "".join(
            f"<tr><th {th}>{escape(k)}</th><td {td}>"
            + (f"<b>{escape(v)}</b>" if k == "AMOUNT TO TRANSFER" else escape(v)) + "</td></tr>"
            for k, v in rows)
        parts.append(f"<h3 style='margin:16px 0 4px;font-size:14px'>{escape(title)}</h3>"
                     f"<table style='border-collapse:collapse;font-size:13px'>{body}</table>")
    return "".join(parts)


def _fill(template: str, values: dict) -> str:
    """Replace known ``{placeholders}`` only; anything else is left as written (no format-string evaluation)."""
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def compose(r: ExternalRefund, *, mode: str, s: ExternalServicesSettings | None = None) -> tuple[str, str, str]:
    s = s or ExternalServicesSettings.load()
    c = context(r)
    flat = {k: v for k, v in c.items() if isinstance(v, str | int)}
    subject = " ".join(_fill(s.refund_email_subject.strip() or DEFAULT_SUBJECT, flat).split())[:240]
    body = s.refund_email_body.strip() or DEFAULT_BODY
    if "{details}" not in body:
        body = body + "\n\n{details}"
    text = _fill(body, {**flat, "details": details_text(c)})
    before, _, after = body.partition("{details}")
    html_part = lambda t: "".join(f"<p>{escape(p).replace(chr(10), '<br/>')}</p>"  # noqa: E731
                                  for p in _fill(t, flat).strip().split("\n\n") if p.strip())
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#111'>{html_part(before)}"
            f"{details_html(c)}{html_part(after)}</div>")
    if mode == "test":
        subject = f"[TEST] {subject}"
        notice = "TEST — not a real refund request. Sent by the portal in test mode."
        text = f"{notice}\n\n{text}"
        html = f"<p style='color:#b45309'><b>{notice}</b></p>{html}"
    return subject, text, html


# --- dispatch ----------------------------------------------------------------------------------------------------------

def due_for_dispatch(s: ExternalServicesSettings):
    """Confirmed refunds that should go out now (QUEUED in immediate mode or batched; FAILED retries)."""
    from django.db.models import Q

    qs = ExternalRefund.objects.filter(destination=RefundDestination.BANK, account_confirmed_at__isnull=False,
                                       attempts__lt=MAX_ATTEMPTS)
    ready = Q(status=RefundStatus.FAILED)
    if s.refund_dispatch_timing == "immediate":
        ready |= Q(status=RefundStatus.QUEUED)
    else:
        ready |= Q(status=RefundStatus.QUEUED, batch__isnull=False)
    return qs.filter(ready)


def mark_stale(now=None) -> int:
    now = now or timezone.now()
    return ExternalRefund.objects.filter(status=RefundStatus.SENDING, claimed_at__lt=now - STALE_SENDING).update(
        status=RefundStatus.UNCERTAIN, last_error="Sending was interrupted; check with SRIC before resending.")


def send(refund_id: int, *, force: bool = False) -> str:
    """Email one refund to SRIC. Returns the resulting status (or why nothing was sent)."""
    s = ExternalServicesSettings.load()
    to, cc = recipients(s)
    if not to:
        return "off" if s.sric_dispatch_mode == "off" else "no_recipient"
    allowed = [RefundStatus.QUEUED, RefundStatus.FAILED] + ([RefundStatus.SENT, RefundStatus.UNCERTAIN] if force else [])
    now = timezone.now()
    claimed = ExternalRefund.objects.filter(
        pk=refund_id, destination=RefundDestination.BANK, account_confirmed_at__isnull=False, status__in=allowed,
    ).update(status=RefundStatus.SENDING, claimed_at=now)
    if not claimed:
        return "not_due"
    r = ExternalRefund.objects.select_related("booking__equipment__internal_department", "user").get(pk=refund_id)
    try:
        subject, text, html = compose(r, mode=s.sric_dispatch_mode, s=s)
        msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, to, cc=cc)
        msg.attach_alternative(html, "text/html")
        if msg.send(fail_silently=False) != 1:
            raise RuntimeError("mail server did not accept the message")
    except Exception as exc:  # noqa: BLE001 - recorded; retried by the batch runs; staff see FAILED
        ExternalRefund.objects.filter(pk=r.pk, status=RefundStatus.SENDING).update(
            status=RefundStatus.FAILED, attempts=r.attempts + 1, last_error=type(exc).__name__[:255])
        logger.warning("bank refund %s: SRIC email failed: %s", r.pk, type(exc).__name__)
        if r.attempts + 1 >= MAX_ATTEMPTS:
            _alert_staff_failed(r)
        return RefundStatus.FAILED
    ExternalRefund.objects.filter(pk=r.pk, status=RefundStatus.SENDING).update(
        status=RefundStatus.SENT, attempts=r.attempts + 1, sent_at=timezone.now(), sent_to=", ".join(to + cc)[:254],
        sent_in_mode=s.sric_dispatch_mode, last_error="", sric_subject=subject[:255], sric_body=text)
    return RefundStatus.SENT


def _alert_staff_failed(r: ExternalRefund) -> None:
    from .invoices import _staff_recipients
    from .notifications import notify

    notify(f"refund:{r.pk}:send_gave_up", _staff_recipients(r.booking),
           title=f"Refund request for {r.booking.virtual_booking_id or r.booking_id} could not be emailed to SRIC",
           message="The portal tried several times and stopped. Check the SRIC refund addresses in the external "
                   "services settings, then resend it from Refunds to bank accounts.",
           link=f"/accounts/refunds?refund={r.pk}")


def schedule_send(refund: ExternalRefund) -> None:
    """After commit: send at once when the Main Administrator chose immediate dispatch."""
    s = ExternalServicesSettings.load()
    if s.refund_dispatch_timing != "immediate" or s.sric_dispatch_mode == "off":
        return
    pk = refund.pk

    def _go():
        try:
            from .tasks import send_bank_refund

            send_bank_refund.delay(pk)
        except Exception:  # noqa: BLE001 - the batch runs retry anything still queued
            logger.warning("could not queue bank refund %s for sending; the batch run will send it", pk)

    transaction.on_commit(_go)


# --- the user's confirmation --------------------------------------------------------------------------------------------

def _notify_staff_confirmed(r: ExternalRefund) -> None:
    from .invoices import _staff_recipients
    from .notifications import notify

    s = ExternalServicesSettings.load()
    when = ("is being emailed to SRIC now" if s.refund_dispatch_timing == "immediate"
            else "goes to SRIC with the weekly batch")
    ref = r.booking.virtual_booking_id or r.booking_id
    notify(f"refund:{r.pk}:staff", _staff_recipients(r.booking), title=f"Bank refund for booking {ref}",
           message=f"Rs. {_rs(r.refund_amount)} for booking {ref} ({getattr(r.booking.equipment, 'name', '')}) is to be "
                   f"paid to the user's bank account. The user confirmed the account; the request {when}. Record the "
                   "settlement once SRIC confirms the transfer.",
           link=f"/accounts/refunds?refund={r.pk}")


def attach_confirmed_account(r: ExternalRefund, account: RefundBankAccount, *, confirmed: bool) -> None:
    """Set the account on a refund being recorded or awaiting one. ``confirmed`` must be the user's explicit tick."""
    from .refunds import account_dict

    if not confirmed:
        raise BankRefundError("Please review the bank account details and confirm that they are correct.",
                              code="confirmation_required")
    r.bank_account = account
    r.bank_snapshot = account_dict(account, full=True)
    r.account_confirmed_at = timezone.now()
    r.status = RefundStatus.QUEUED


def confirm_account(user, refund_id, *, bank_account_id, confirmed: bool) -> ExternalRefund:
    """The user gives (and confirms) the account for a refund the lab recorded to a bank account."""
    with transaction.atomic():
        r = (ExternalRefund.objects.select_for_update(of=("self",)).select_related("booking__equipment")
             .filter(pk=refund_id, user=user, destination=RefundDestination.BANK).first())
        if r is None:
            raise BankRefundError("Refund not found.", status=404, code="not_found")
        if r.status != RefundStatus.AWAITING_ACCOUNT:
            raise BankRefundError("The bank account for this refund has already been confirmed.", status=409,
                                  code="already_confirmed")
        account = RefundBankAccount.objects.filter(pk=bank_account_id, user=user, is_active=True).first()
        if account is None:
            raise BankRefundError("Choose or add the bank account for the refund.", code="account_required")
        attach_confirmed_account(r, account, confirmed=confirmed)
        r.save(update_fields=["bank_account", "bank_snapshot", "account_confirmed_at", "status", "updated_at"])
        _notify_staff_confirmed(r)
        schedule_send(r)
    return r


# --- staff ----------------------------------------------------------------------------------------------------------------

def may_handle(user, refund: ExternalRefund | None = None) -> bool:
    """The Main Administrator, or an Accounts In Charge (of the booking's department when a refund is given)."""
    from iic_booking.department_modules.access import is_main_admin
    from iic_booking.users.models.user_type import UserType

    if is_main_admin(user):
        return True
    if getattr(user, "user_type", None) != UserType.FINANCE or not getattr(user, "is_active", False):
        return False
    if refund is None:
        return True
    from .invoices import staff_may_handle

    return staff_may_handle(user, refund.booking)


def _staff_refund(user, refund_id, *, lock: bool) -> ExternalRefund:
    qs = ExternalRefund.objects.select_related("booking__equipment__internal_department", "user").filter(
        pk=refund_id, destination=RefundDestination.BANK)
    r = (qs.select_for_update(of=("self",)) if lock else qs).first()
    if r is None:
        raise BankRefundError("Refund not found.", status=404, code="not_found")
    if not may_handle(user, r):
        raise BankRefundError("Only the Accounts In Charge of this department or the Main Administrator can handle "
                              "this refund.", status=403, code="forbidden")
    return r


def staff_detail(user, refund_id) -> dict:
    from .refunds import refund_dict

    r = _staff_refund(user, refund_id, lock=False)
    s = ExternalServicesSettings.load()
    c = context(r)
    to, cc = recipients(s)
    out = {**refund_dict(r, staff=True), "context": {k: v for k, v in c.items()}, "sric_subject": r.sric_subject,
           "sric_body": r.sric_body, "sent_to": r.sent_to, "sent_in_mode": r.sent_in_mode, "attempts": r.attempts,
           "last_error": r.last_error, "dispatch": {"mode": s.sric_dispatch_mode, "timing": s.refund_dispatch_timing,
                                                    "to": to, "cc": cc}}
    if not r.sric_body and r.bank_snapshot:
        subject, text, _html = compose(r, mode=s.sric_dispatch_mode, s=s)
        out["preview_subject"], out["preview_body"] = subject, text
    return out


def resend(user, refund_id, *, note: str = "") -> dict:
    """Send (again) on staff request: a failed, interrupted, or already-sent request (for example SRIC lost it)."""
    r = _staff_refund(user, refund_id, lock=False)
    if r.status == RefundStatus.AWAITING_ACCOUNT:
        raise BankRefundError("The user has not confirmed a bank account yet.", code="awaiting_account")
    if r.status == RefundStatus.CLOSED:
        raise BankRefundError("This refund is already settled.", status=409, code="closed")
    if r.status == RefundStatus.SENDING:
        raise BankRefundError("This request is being sent right now.", status=409, code="sending")
    s = ExternalServicesSettings.load()
    if not recipients(s)[0]:
        raise BankRefundError("Sending to SRIC is off or has no address. Set it in the external services settings.",
                              code="dispatch_off")
    if r.status == RefundStatus.FAILED and r.attempts >= MAX_ATTEMPTS:
        ExternalRefund.objects.filter(pk=r.pk).update(attempts=MAX_ATTEMPTS - 1)
    result = send(r.pk, force=True)
    if result != RefundStatus.SENT:
        raise BankRefundError("The email to SRIC could not be sent. Please try again later.", status=502,
                              code="send_failed")
    logger.info("bank refund %s resent by user %s %s", r.pk, user.pk, (note or "")[:100])
    r.refresh_from_db()
    return staff_detail(user, r.pk)


def settle(user, refund_id, *, amount=None, settled_on=None, mode: str = "", utr: str = "", note: str = "") -> ExternalRefund:
    """Record that SRIC transferred the money. The user is told the amount and every deduction."""
    from .notifications import notify

    utr = str(utr or "").strip()[:64]
    note = str(note or "").strip()[:1000]
    mode = str(mode or "").strip().lower()
    if mode and mode not in SettlementMode.values:
        raise BankRefundError("Choose how the amount was paid.", code="invalid_mode")
    if not utr and len(note) < 5:
        raise BankRefundError("Give the bank reference (UTR) or a note on how the payment was confirmed.",
                              code="proof_required")
    on = timezone.localdate()
    if settled_on not in (None, ""):
        try:
            on = date.fromisoformat(str(settled_on)[:10])
        except ValueError:
            raise BankRefundError("Enter the payment date as YYYY-MM-DD.", code="invalid_date") from None
    if on > timezone.localdate():
        raise BankRefundError("The payment date cannot be in the future.", code="invalid_date")
    with transaction.atomic():
        r = _staff_refund(user, refund_id, lock=True)
        if r.status == RefundStatus.CLOSED:
            raise BankRefundError("This refund is already settled.", status=409, code="closed")
        if r.status == RefundStatus.AWAITING_ACCOUNT:
            raise BankRefundError("The user has not confirmed a bank account yet.", code="awaiting_account")
        paid = _m(r.refund_amount) if amount in (None, "") else _m(amount)
        if paid <= 0 or paid > _m(r.refund_amount):
            raise BankRefundError(f"The amount paid must be more than zero and at most Rs. {_rs(r.refund_amount)}.",
                                  code="invalid_amount")
        if on < timezone.localtime(r.created_at).date():
            raise BankRefundError("The payment date cannot be before the refund was recorded.", code="invalid_date")
        r.status, r.utr, r.close_note = RefundStatus.CLOSED, utr, note
        r.settled_amount, r.settled_on, r.settlement_mode = paid, on, mode
        r.closed_at, r.closed_by = timezone.now(), user
        r.save(update_fields=["status", "utr", "close_note", "settled_amount", "settled_on", "settlement_mode",
                              "closed_at", "closed_by", "updated_at"])
        ref = r.booking.virtual_booking_id or r.booking_id
        notify(f"refund:{r.pk}:closed", [r.user], title=f"Refund for booking {ref} paid to your bank account",
               message=settlement_message(r), link=f"/my-refunds?refund={r.pk}")
    return r


def settlement_message(r: ExternalRefund) -> str:
    bank = r.bank_snapshot or {}
    from .refunds import masked

    head = (f"Rs. {_rs(r.settled_amount or r.refund_amount)} for booking {r.booking.virtual_booking_id or r.booking_id} "
            f"has been paid to your account {bank.get('bank_name', '')} {masked(bank.get('account_number', ''))}")
    if r.settled_on:
        head += f" on {r.settled_on:%d %b %Y}"
    lines = [head + (f" (bank reference {r.utr})." if r.utr else ".")]
    lines.append(f"Amount paid for the booking: Rs. {_rs(r.paid_amount)}.")
    ded = deductions(r)
    if ded:
        lines.append("Deductions: " + "; ".join(f"{d['label']}: Rs. {_rs(d['amount'])}" for d in ded) + ".")
    else:
        lines.append("Nothing was deducted.")
    label = entitlement(r)["label"]
    lines.append(f"Refund: Rs. {_rs(r.refund_base)} plus GST Rs. {_rs(r.refund_gst)} = Rs. {_rs(r.refund_amount)} "
                 f"({label[0].lower()}{label[1:]}).")
    if r.settled_amount is not None and _m(r.settled_amount) < _m(r.refund_amount):
        lines.append(f"Rs. {_rs(_m(r.refund_amount) - _m(r.settled_amount))} of the refund is still to be paid; the "
                     "Accounts In Charge will contact you.")
    return " ".join(lines)


def whatsapp_deductions(r: ExternalRefund) -> str:
    ded = deductions(r)
    if not ded:
        return "None"
    short = {"service": "Cancellation penalty" if r.penalty_percent else "Service charges not refunded",
             "shipping": "Return shipping", "gst": "GST (invoice issued)" if not r.gst_refunded else "GST"}
    return ", ".join(f"{short.get(d['code'], d['label'])} Rs. {_rs(d['amount'])}" for d in ded)


# --- the Main Administrator's settings ------------------------------------------------------------------------------------

SETTINGS_FIELDS = ("refund_sric_recipients", "refund_sric_cc", "refund_sric_test_recipients", "refund_dispatch_timing",
                   "refund_email_subject", "refund_email_body")


def settings_dict(s: ExternalServicesSettings | None = None) -> dict:
    s = s or ExternalServicesSettings.load()
    to, cc = recipients(s)
    return {**{f: getattr(s, f) for f in SETTINGS_FIELDS}, "sric_dispatch_mode": s.sric_dispatch_mode,
            "effective_to": to, "effective_cc": cc, "default_subject": DEFAULT_SUBJECT, "default_body": DEFAULT_BODY,
            "placeholders": PLACEHOLDERS}


def clean_settings(data: dict) -> dict:
    """Validated values for the refund fields present in ``data`` (used by the settings endpoint)."""
    out = {}
    for field, label in (("refund_sric_recipients", "SRIC refund addresses"), ("refund_sric_cc", "Copy to"),
                         ("refund_sric_test_recipients", "Test addresses")):
        if field in data:
            out[field] = validate_addresses(data.get(field) or "", label)
    if "refund_dispatch_timing" in data:
        timing = str(data.get("refund_dispatch_timing") or "").strip()
        if timing not in ("immediate", "weekly"):
            raise BankRefundError("Choose when refund requests go to SRIC.", code="invalid_timing")
        out["refund_dispatch_timing"] = timing
    for field, limit in (("refund_email_subject", 255), ("refund_email_body", 10000)):
        if field in data:
            value = str(data.get(field) or "").strip()
            if len(value) > limit:
                raise BankRefundError(f"The {field.split('_')[-1]} is too long (at most {limit} characters).",
                                      code="too_long")
            unknown = sorted({m for m in _PLACEHOLDER.findall(value) if m not in PLACEHOLDERS})
            if unknown:
                raise BankRefundError(f"Unknown placeholder {{{unknown[0]}}}. Use only the ones listed.",
                                      code="unknown_placeholder")
            out[field] = value
    return out


def preview(user, data: dict) -> dict:
    """The email as it would go out with the given (unsaved) wording, on the latest bank refund or a sample."""
    from iic_booking.department_modules.access import is_main_admin

    if not is_main_admin(user):
        raise BankRefundError("Only the Main Administrator can edit the SRIC refund email.", status=403, code="forbidden")
    s = ExternalServicesSettings.load()
    for field, value in clean_settings(data).items():
        setattr(s, field, value)
    r = (ExternalRefund.objects.filter(destination=RefundDestination.BANK).exclude(bank_snapshot={})
         .select_related("booking__equipment__internal_department", "user").order_by("-created_at").first())
    if r is None:
        return {"subject": _fill(s.refund_email_subject or DEFAULT_SUBJECT, _sample()), "sample": True,
                "body": _fill(s.refund_email_body or DEFAULT_BODY, {**_sample(), "details": "(the details block)"})}
    subject, text, html = compose(r, mode=s.sric_dispatch_mode if s.sric_dispatch_mode != "off" else "live", s=s)
    return {"subject": subject, "body": text, "html": html, "sample": False,
            "based_on": r.booking.virtual_booking_id or r.booking_id}


def _sample() -> dict:
    return {"booking_reference": "XRD202600123", "user_name": "Asha Verma", "refund_amount": "1,180.00",
            "entitlement": "Full refund: service charges with GST", "equipment": "X-Ray Diffractometer",
            "department": "Institute Instrumentation Centre"}
