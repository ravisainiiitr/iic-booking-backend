"""Refunds for external users: cancellation penalties, disruption refunds and refunds to a bank account.

Policy (31 Oct 2026 committee; applies while ``ExternalServicesSettings.cancellation_policy_enabled`` is on, to
external users under the external workflow, test accounts only while piloted):

* The user cancels the booking themselves at any time before the first slot starts:
  more than ``cancellation_free_hours`` (48) before -> no penalty; from then until ``cancellation_half_hours`` (24)
  before -> ``cancellation_half_penalty_percent`` (50) % penalty; less than that -> 100 % penalty (no refund).
  The refund goes to the wallet at once.
* A lab disruption (equipment down, operator absent, other) carries no penalty. The user chooses the wallet (at
  once) or a bank account; a bank refund is queued for SRIC and sent with the invoice requests on the first working
  day of the week. The account is saved for next time.
* What is refunded: the amount paid minus the non-refundable return shipping charge (and its GST). If a GST invoice
  has already been requested for the booking, the GST is not refunded here (SRIC settles the tax with the invoice);
  otherwise the GST on the refunded part is returned too. The user sees the figures before confirming.
* The Accounts In Charge follow every bank refund until it is closed, which they do once the money has reached the
  user's account.

Every refund is recorded once per booking (``ExternalRefund``, unique booking); nothing here changes a past
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import ExternalRefund, ExternalServicesSettings, RefundBankAccount, RefundDestination, RefundOrigin, RefundStatus
from .notifications import notify
from .window import is_external_booking_user

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
ACTIVE_STATUSES = ("PENDING", "BOOKED")


class RefundPolicyError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", extra=None):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code
        self.extra = extra or {}

    def payload(self) -> dict:
        return {"error": self.message, "code": self.code, **self.extra}


def _money(value) -> Decimal:
    return Decimal(str(value or "0")).quantize(CENT, rounding=ROUND_HALF_UP)


def _settings() -> ExternalServicesSettings:
    return ExternalServicesSettings.load()


def policy_applies(user, s: ExternalServicesSettings | None = None) -> bool:
    s = s or _settings()
    return bool(s.cancellation_policy_enabled) and is_external_booking_user(user)


def first_slot_start(booking):
    return booking.daily_slots.order_by("start_datetime").values_list("start_datetime", flat=True).first()


def _local(dt) -> str:
    return f"{timezone.localtime(dt):%d %b %Y, %H:%M}"


# --- amounts ---------------------------------------------------------------------------------------------------------

def _gst_in_breakdown(booking) -> Decimal:
    rows = getattr(booking, "charge_breakdown", None) or []
    total = ZERO
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("description", "")).strip().upper().startswith("GST"):
            total += _money(row.get("amount"))
    return total


def invoice_requested(booking) -> bool:
    from .models import SricInvoiceRequest

    return SricInvoiceRequest.objects.filter(booking=booking).exists()


@dataclass(frozen=True)
class RefundQuote:
    paid: Decimal                 # what the user paid towards the booking (GST included)
    gst_paid: Decimal             # GST inside ``paid``
    shipping: Decimal             # non-refundable return shipping, GST included
    refundable_base: Decimal      # paid - GST - shipping (before GST)
    penalty_percent: int
    gst_refunded: bool            # False once a GST invoice has been requested
    refund_base: Decimal          # refundable_base after the penalty
    refund_gst: Decimal           # GST returned with it (0 when gst_refunded is False)
    refund: Decimal               # refund_base + refund_gst
    tier: str                     # free | half | full | disruption

    def as_dict(self) -> dict:
        return {
            "paid": str(self.paid), "gst_paid": str(self.gst_paid), "return_shipping": str(self.shipping),
            "refundable_base": str(self.refundable_base), "penalty_percent": self.penalty_percent,
            "gst_refunded": self.gst_refunded, "refund_base": str(self.refund_base),
            "refund_gst": str(self.refund_gst), "refund": str(self.refund), "tier": self.tier,
        }


def quote(booking, *, penalty_percent: int, tier: str) -> RefundQuote:
    from iic_booking.equipment.booking_paid_amount import booking_paid_charge

    paid = _money(booking_paid_charge(booking))
    gst = min(_gst_in_breakdown(booking), paid)
    taxable = paid - gst
    rate = (gst / taxable) if taxable > 0 else ZERO
    ship_base = min(_money(getattr(booking, "return_shipping_fee_amount", None)), taxable)
    shipping = _money(ship_base * (1 + rate))
    refundable_base = max(ZERO, taxable - ship_base)
    keep = Decimal(100 - penalty_percent) / Decimal(100)
    refund_base = _money(refundable_base * keep)
    gst_refunded = not invoice_requested(booking)
    refund_gst = _money(refund_base * rate) if gst_refunded else ZERO
    refund = min(paid, refund_base + refund_gst)
    return RefundQuote(paid, gst, shipping, refundable_base, penalty_percent, gst_refunded, refund_base, refund_gst,
                       refund, tier)


def cancellation_tier(start, now=None, s: ExternalServicesSettings | None = None) -> tuple[str, int]:
    """(tier, penalty percent) for a user cancellation with the first slot at ``start``."""
    s = s or _settings()
    now = now or timezone.now()
    left = start - now
    if left > timedelta(hours=int(s.cancellation_free_hours or 48)):
        return "free", 0
    if left > timedelta(hours=int(s.cancellation_half_hours or 24)):
        return "half", int(s.cancellation_half_penalty_percent)
    return "full", 100


def is_disrupted(booking) -> bool:
    from iic_booking.equipment.fabrication_workflow import is_rejection_active
    from iic_booking.equipment.models import BookingStatus

    return (
        booking.status == BookingStatus.DISRUPTION_PENDING
        or bool(getattr(booking, "maintenance_disruption_flag", False))
        or is_rejection_active(booking)
    )


def cancellation_quote(booking, *, now=None) -> dict:
    """What the owner would get back by cancelling now (shown before they confirm)."""
    s = _settings()
    start = first_slot_start(booking)
    if is_disrupted(booking):
        q = quote(booking, penalty_percent=0, tier="disruption")
        return {"disruption": True, "bank_refund_allowed": True, **q.as_dict(), "first_slot_start": _iso(start)}
    if start is None:
        raise RefundPolicyError("Start time is not available.", code="no_start_time")
    now = now or timezone.now()
    if now >= start:
        raise RefundPolicyError("The booking has started and can no longer be cancelled here.", code="started")
    tier, percent = cancellation_tier(start, now, s)
    q = quote(booking, penalty_percent=percent, tier=tier)
    hours = int(s.cancellation_free_hours or 48)
    half = int(s.cancellation_half_hours or 24)
    return {
        "disruption": False, "bank_refund_allowed": False, **q.as_dict(), "first_slot_start": _iso(start),
        "free_until": _iso(start - timedelta(hours=hours)), "half_until": _iso(start - timedelta(hours=half)),
        "rules": {"free_hours": hours, "half_hours": half, "half_penalty_percent": int(s.cancellation_half_penalty_percent)},
    }


def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


# --- bank accounts ---------------------------------------------------------------------------------------------------

def masked(number: str) -> str:
    digits = str(number or "")
    return ("X" * max(0, len(digits) - 4)) + digits[-4:] if digits else ""


def account_dict(acc: RefundBankAccount, *, full: bool = False) -> dict:
    return {
        "id": acc.pk, "account_holder_name": acc.account_holder_name, "bank_name": acc.bank_name,
        "ifsc": acc.ifsc, "branch": acc.branch,
        "account_number": acc.account_number if full else masked(acc.account_number),
        "is_default": acc.is_default, "created_at": acc.created_at.isoformat() if acc.created_at else None,
    }


def save_account(user, data: dict) -> RefundBankAccount:
    import re

    holder = str(data.get("account_holder_name") or "").strip()[:150]
    bank = str(data.get("bank_name") or "").strip()[:150]
    number = re.sub(r"\s+", "", str(data.get("account_number") or ""))
    confirm = re.sub(r"\s+", "", str(data.get("account_number_confirm") or number))
    ifsc = str(data.get("ifsc") or "").strip().upper()
    branch = str(data.get("branch") or "").strip()[:150]
    errors = {}
    if len(holder) < 2:
        errors["account_holder_name"] = "Enter the account holder's name."
    if len(bank) < 2:
        errors["bank_name"] = "Enter the bank's name."
    if not re.fullmatch(r"\d{9,18}", number):
        errors["account_number"] = "Enter the account number (9 to 18 digits)."
    elif number != confirm:
        errors["account_number_confirm"] = "The account numbers do not match."
    if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        errors["ifsc"] = "Enter the 11-character IFSC (for example SBIN0001234)."
    if errors:
        raise RefundPolicyError("Please check the bank account details.", code="invalid_account", extra={"fields": errors})
    with transaction.atomic():
        existing = RefundBankAccount.objects.filter(user=user, account_number=number, ifsc=ifsc, is_active=True).first()
        if existing:
            existing.account_holder_name, existing.bank_name, existing.branch = holder, bank, branch
            existing.save(update_fields=["account_holder_name", "bank_name", "branch", "updated_at"])
            acc = existing
        else:
            acc = RefundBankAccount.objects.create(user=user, account_holder_name=holder, bank_name=bank,
                                                   account_number=number, ifsc=ifsc, branch=branch)
        RefundBankAccount.objects.filter(user=user).exclude(pk=acc.pk).update(is_default=False)
        if not acc.is_default:
            acc.is_default = True
            acc.save(update_fields=["is_default", "updated_at"])
    return acc


def my_accounts(user) -> list[dict]:
    return [account_dict(a) for a in RefundBankAccount.objects.filter(user=user, is_active=True).order_by("-is_default", "-created_at")]


# --- cancelling --------------------------------------------------------------------------------------------------------

def _record(booking, user, *, origin, destination, q: RefundQuote, account=None, txn=None, actor=None) -> ExternalRefund:
    snapshot = account_dict(account, full=True) if account is not None else {}
    status = RefundStatus.QUEUED if destination == RefundDestination.BANK else RefundStatus.CLOSED
    try:
        with transaction.atomic():
            return ExternalRefund.objects.create(
                booking=booking, user=user, origin=origin, destination=destination, tier=q.tier,
                penalty_percent=q.penalty_percent, paid_amount=q.paid, gst_paid=q.gst_paid,
                return_shipping=q.shipping, refund_base=q.refund_base, refund_gst=q.refund_gst,
                refund_amount=q.refund, gst_refunded=q.gst_refunded, bank_account=account,
                bank_snapshot=snapshot, wallet_transaction=txn, status=status, created_by=actor or user,
                closed_at=timezone.now() if status == RefundStatus.CLOSED else None,
            )
    except IntegrityError as exc:
        raise RefundPolicyError("This booking has already been refunded.", status=409, code="already_refunded") from exc


def cancel(user, booking_id, *, destination: str = "wallet", bank_account_id=None, notes: str = "",
           expected_refund=None) -> dict:
    """The owner cancels a whole booking under the policy. ``expected_refund`` (the amount the user was shown) must
    match the amount computed now, so a tier boundary crossed while the dialog was open is never a surprise."""
    from iic_booking.communication.utils import booking_display_id_for_email
    from iic_booking.equipment.booking_cancellation import perform_booking_cancellation
    from iic_booking.equipment.booking_events import create_booking_event
    from iic_booking.equipment.models import Booking, BookingEventType, BookingStatus
    from iic_booking.equipment.reschedule_lock import cancel_locked_for, cancel_locked_payload
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    s = _settings()
    if not policy_applies(user, s):
        raise RefundPolicyError("This cancellation is not available for this account.", status=403, code="not_available")
    destination = str(destination or "wallet").strip().lower()
    if destination not in (RefundDestination.WALLET, RefundDestination.BANK):
        raise RefundPolicyError("Choose where the refund goes.", code="invalid_destination")
    notes = str(notes or "").strip()[:2000]
    with transaction.atomic():
        booking = Booking.objects.select_for_update(of=("self",)).select_related("equipment").filter(pk=booking_id).first()
        if booking is None or booking.user_id != user.pk:
            raise RefundPolicyError("Booking not found.", status=404, code="not_found")
        disrupted = is_disrupted(booking)
        if booking.status not in ACTIVE_STATUSES and booking.status != BookingStatus.DISRUPTION_PENDING:
            raise RefundPolicyError("Only a booked or pending booking can be cancelled.", code="not_active")
        if getattr(booking, "source_booking_id", None) is not None:
            raise RefundPolicyError("Repeat sample bookings cannot be cancelled by users. Please contact the lab.",
                                    code="repeat_booking")
        if not disrupted and cancel_locked_for(user, booking):
            raise RefundPolicyError(cancel_locked_payload().get("error", "This booking cannot be cancelled."), code="locked")
        if ExternalRefund.objects.filter(booking=booking).exists():
            raise RefundPolicyError("This booking has already been refunded.", status=409, code="already_refunded")
        info = cancellation_quote(booking)
        if destination == RefundDestination.BANK and not info["bank_refund_allowed"]:
            raise RefundPolicyError("A refund to a bank account is offered only when the lab disrupts the booking.",
                                    code="bank_not_allowed")
        if expected_refund is not None and _money(expected_refund) != _money(info["refund"]):
            raise RefundPolicyError("The refund amount has changed since it was shown. Please check it again.",
                                    status=409, code="quote_changed", extra={"quote": info})
        account = None
        if destination == RefundDestination.BANK:
            account = RefundBankAccount.objects.filter(pk=bank_account_id, user=user, is_active=True).first()
            if account is None:
                raise RefundPolicyError("Choose or add the bank account for the refund.", code="account_required")
        q = quote(booking, penalty_percent=int(info["penalty_percent"]), tier=info["tier"])
        ref = booking_display_id_for_email(booking)
        eq_name = getattr(booking.equipment, "name", "")
        if q.tier == "disruption":
            reason = "Cancelled after a lab disruption: no penalty."
        elif q.penalty_percent:
            reason = f"Cancelled by the user with a {q.penalty_percent}% cancellation penalty."
        else:
            reason = "Cancelled by the user more than the free-cancellation period before the booking."
        note = " ".join(part for part in (reason, notes) if part)
        previous = booking.status
        perform_booking_cancellation(
            booking, slot_ids=list(booking.daily_slots.values_list("id", flat=True)), should_refund=False,
            cancel_notes=note, actor=user, allow_started_slots=disrupted, cancelled_by_label="user",
        )
        txn = None
        if destination == RefundDestination.WALLET and q.refund > 0:
            target, _ = WalletRepository.get_booking_wallet_target(booking.user, getattr(booking.equipment, "internal_department", None))
            if target is None:
                raise RefundPolicyError("No wallet was found for this department to refund to.", status=409, code="no_wallet")
            txn = target.credit(
                amount=q.refund,
                description=f"Refund for cancelled Booking {ref}- {eq_name} ({_refund_label(q)})",
                related_user=booking.user,
            )
        refund = _record(booking, user, origin=RefundOrigin.DISRUPTION if q.tier == "disruption" else RefundOrigin.CANCELLATION,
                         destination=destination, q=q, account=account, txn=txn)
        booking.refresh_from_db()
        if q.refund > 0 and destination == RefundDestination.WALLET:
            booking.status = BookingStatus.REFUNDED
            booking.save(update_fields=["status"])
        create_booking_event(
            booking=booking, event_type=BookingEventType.REFUNDED if txn else BookingEventType.CANCELLED,
            created_by=user, previous_status=previous, new_status=booking.status,
            comment=_user_message(q, destination, ref),
            metadata={"external_refund_id": refund.pk, **q.as_dict(), "destination": destination},
            send_notification=False,
        )
        notify(f"refund:{refund.pk}:created", [user], title=f"Booking {ref} cancelled",
               message=_user_message(q, destination, ref), link=f"/my-bookings?booking={booking.pk}")
        if destination == RefundDestination.BANK and q.refund > 0:
            from .invoices import _staff_recipients

            notify(f"refund:{refund.pk}:staff", _staff_recipients(booking),
                   title=f"Bank refund queued for booking {ref}",
                   message=f"₹{q.refund} for booking {ref} ({eq_name}) is to be refunded to the user's bank account "
                           "after a lab disruption. It goes to SRIC with the weekly batch; close it once SRIC confirms "
                           "the payment.",
                   link="/accounts/refunds")
    return {"refund": refund_dict(refund), "booking_status": booking.status}


def _refund_label(q: RefundQuote) -> str:
    if q.tier == "disruption":
        return "lab disruption, no penalty"
    if q.penalty_percent:
        return f"{q.penalty_percent}% cancellation penalty"
    return "no penalty"


def _user_message(q: RefundQuote, destination: str, ref) -> str:
    if q.refund <= 0:
        return (f"Booking {ref} was cancelled less than the minimum notice before it started, so no refund is due "
                f"({q.penalty_percent}% cancellation penalty).")
    gst = "" if q.gst_refunded else " GST is not included because a GST invoice was already requested; SRIC settles it."
    ship = f" The return shipping charge (₹{q.shipping}) is not refundable." if q.shipping > 0 else ""
    where = ("has been credited to your wallet" if destination == RefundDestination.WALLET
             else "will be paid to your bank account through SRIC; you will be told when it is paid")
    return f"₹{q.refund} for booking {ref} {where} ({_refund_label(q)}).{ship}{gst}"


# --- views for the user and the Accounts In Charge ----------------------------------------------------------------------

def refund_dict(r: ExternalRefund, *, staff: bool = False) -> dict:
    out = {
        "id": r.pk, "booking_id": r.booking_id,
        "booking_reference": getattr(r.booking, "virtual_booking_id", "") or str(r.booking_id),
        "origin": r.origin, "destination": r.destination, "tier": r.tier, "penalty_percent": r.penalty_percent,
        "paid_amount": str(r.paid_amount), "return_shipping": str(r.return_shipping),
        "refund_base": str(r.refund_base), "refund_gst": str(r.refund_gst), "refund_amount": str(r.refund_amount),
        "gst_refunded": r.gst_refunded, "status": r.status, "status_label": r.get_status_display(),
        "created_at": r.created_at.isoformat(), "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None, "close_note": r.close_note,
        "bank": ({**r.bank_snapshot, "account_number": masked(r.bank_snapshot.get("account_number", ""))}
                 if r.bank_snapshot and not staff else (r.bank_snapshot or None)),
    }
    if staff:
        from iic_booking.users.display import get_user_display_name

        out["user"] = {"id": r.user_id, "name": get_user_display_name(r.user), "email": r.user.email}
        out["equipment"] = getattr(r.booking.equipment, "name", "")
        out["utr"] = r.utr
    return out


def my_refunds(user) -> list[dict]:
    qs = ExternalRefund.objects.filter(user=user).select_related("booking").order_by("-created_at")
    return [refund_dict(r) for r in qs[:100]]


def staff_may_track(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return getattr(user, "user_type", None) in (UserType.FINANCE, UserType.ADMIN)


def tracker(user, *, status: str = "", q: str = "") -> list[dict]:
    if not staff_may_track(user):
        raise RefundPolicyError("Only the Accounts In Charge can see refunds.", status=403, code="forbidden")
    qs = ExternalRefund.objects.filter(destination=RefundDestination.BANK).select_related("booking__equipment", "user")
    if status:
        qs = qs.filter(status=status)
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(booking__virtual_booking_id__icontains=q) | Q(user__email__icontains=q) | Q(utr__icontains=q))
    return [refund_dict(r, staff=True) for r in qs.order_by("status", "-created_at")[:300]]


def close(user, refund_id, *, utr: str = "", note: str = "") -> ExternalRefund:
    """The Accounts In Charge confirm that the money reached the user's account."""
    if not staff_may_track(user):
        raise RefundPolicyError("Only the Accounts In Charge can close refunds.", status=403, code="forbidden")
    utr = str(utr or "").strip()[:64]
    note = str(note or "").strip()[:1000]
    if not utr and len(note) < 5:
        raise RefundPolicyError("Give the bank reference (UTR) or a note on how the payment was confirmed.",
                                code="proof_required")
    with transaction.atomic():
        r = ExternalRefund.objects.select_for_update().filter(pk=refund_id, destination=RefundDestination.BANK).first()
        if r is None:
            raise RefundPolicyError("Refund not found.", status=404, code="not_found")
        if r.status == RefundStatus.CLOSED:
            raise RefundPolicyError("This refund is already closed.", status=409, code="closed")
        r.status, r.utr, r.close_note = RefundStatus.CLOSED, utr, note
        r.closed_at, r.closed_by = timezone.now(), user
        r.save(update_fields=["status", "utr", "close_note", "closed_at", "closed_by", "updated_at"])
        ref = getattr(r.booking, "virtual_booking_id", "") or r.booking_id
        notify(f"refund:{r.pk}:closed", [r.user], title=f"Refund for booking {ref} paid",
               message=f"₹{r.refund_amount} for booking {ref} has been paid to your bank account"
                       + (f" (reference {utr})." if utr else "."),
               link=f"/my-bookings?booking={r.booking_id}")
    return r
