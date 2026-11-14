"""Refunds for external users: cancellation penalties, disruption refunds and refunds to a bank account.

Policy (31 Oct 2026 committee; applies while ``ExternalServicesSettings.cancellation_policy_enabled`` is on, to
external users under the external workflow, test accounts only while piloted):

* The user cancels the booking themselves at any time before the first slot starts:
  more than ``cancellation_free_hours`` (48) before -> no penalty; from then until ``cancellation_half_hours`` (24)
  before -> ``cancellation_half_penalty_percent`` (50) % penalty; less than that -> 100 % penalty (no refund).
  The refund goes to the wallet at once.
* A lab disruption (equipment down, operator absent, other) carries no penalty. When the instrument itself is
  non-functional (equipment down / maintenance) the user chooses the wallet (at once) or a bank account.
* Any refund with something to give back may go to the wallet (at once) or to a bank account (14 Nov 2026): the user
  reviews and confirms the account, and the request goes to SRIC (``bank_refunds``). Lab-failure refunds recorded by
  staff to a bank account wait for the user to give and confirm the account.
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
    shipping: Decimal             # return shipping charged, GST included
    refundable_base: Decimal      # paid - GST - shipping (before GST): the service charges
    penalty_percent: int
    gst_refunded: bool            # False once a GST invoice has been requested
    refund_base: Decimal          # service charges refunded (after penalty and portion) + shipping base refunded
    refund_gst: Decimal           # GST returned with it (0 when gst_refunded is False)
    refund: Decimal               # refund_base + refund_gst
    tier: str                     # free | half | full | disruption | lab_failure
    shipping_refunded: Decimal = ZERO  # return shipping given back (base, plus its GST only when gst_refunded)
    portion_percent: int = 100    # share of the service charges refunded
    service_refunded: Decimal = ZERO  # service charges refunded, before GST

    def as_dict(self) -> dict:
        return {
            "paid": str(self.paid), "gst_paid": str(self.gst_paid), "return_shipping": str(self.shipping),
            "refundable_base": str(self.refundable_base), "penalty_percent": self.penalty_percent,
            "gst_refunded": self.gst_refunded, "refund_base": str(self.refund_base),
            "refund_gst": str(self.refund_gst), "refund": str(self.refund), "tier": self.tier,
            "shipping_refunded": str(self.shipping_refunded), "portion_percent": self.portion_percent,
            "service_refunded": str(self.service_refunded),
        }


def quote(booking, *, penalty_percent: int, tier: str, include_shipping: bool = False,
          portion_percent: int = 100) -> RefundQuote:
    """The refund, computed on the server from what was paid.

    * Service charges: ``paid - GST - return shipping``, less the cancellation penalty, times ``portion_percent``.
    * Return shipping (``include_shipping``: the lab is at fault): its base is refunded.
    * GST on the refunded amounts is refunded only while no GST invoice has been requested; once SRIC is asked for an
      invoice, the tax is settled there (credit note), so only base amounts come back (Module 4.2).
    """
    from iic_booking.equipment.booking_paid_amount import booking_paid_charge

    portion_percent = max(0, min(100, int(portion_percent)))
    paid = _money(booking_paid_charge(booking))
    gst = min(_gst_in_breakdown(booking), paid)
    taxable = paid - gst
    rate = (gst / taxable) if taxable > 0 else ZERO
    ship_base = min(_money(getattr(booking, "return_shipping_fee_amount", None)), taxable)
    shipping = _money(ship_base * (1 + rate))
    refundable_base = max(ZERO, taxable - ship_base)
    keep = Decimal(100 - penalty_percent) / Decimal(100) * Decimal(portion_percent) / Decimal(100)
    service_refund = _money(refundable_base * keep)
    ship_refund_base = ship_base if include_shipping else ZERO
    # GST comes back too where the Main Administrator allowed a credit note for this booking's invoice
    # (gst_einvoice.credit_notes); recording the refund then issues the credit note.
    gst_refunded = not invoice_requested(booking) or _credit_note_allowed(booking)
    refund_base = service_refund + ship_refund_base
    refund_gst = _money(refund_base * rate) if gst_refunded else ZERO
    refund = min(paid, refund_base + refund_gst)
    shipping_refunded = _money(ship_refund_base * (1 + rate)) if gst_refunded else ship_refund_base
    return RefundQuote(paid, gst, shipping, refundable_base, penalty_percent, gst_refunded, refund_base, refund_gst,
                       refund, tier, shipping_refunded, portion_percent, service_refund)


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


def instrument_non_functional(booking) -> bool:
    """The disruption is the instrument itself (down or under maintenance): the only case refunded to a bank account.
    Operator unavailable, analysis not possible or rejected files are refunded to the wallet (2 Nov 2026 policy)."""
    from iic_booking.equipment.models import BookingDisruptionKind

    return bool(getattr(booking, "maintenance_disruption_flag", False)) or (
        getattr(booking, "disruption_kind", "") == BookingDisruptionKind.MAINTENANCE)


def cancellation_quote(booking, *, now=None) -> dict:
    """What the owner would get back by cancelling now (shown before they confirm)."""
    s = _settings()
    start = first_slot_start(booking)
    if is_disrupted(booking):
        q = quote(booking, penalty_percent=0, tier="disruption", include_shipping=True)
        return {"disruption": True, "bank_refund_allowed": q.refund > 0, **q.as_dict(),
                "first_slot_start": _iso(start)}
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
        "disruption": False, "bank_refund_allowed": q.refund > 0, **q.as_dict(), "first_slot_start": _iso(start),
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

def _credit_note_allowed(booking) -> bool:
    try:
        from iic_booking.gst_einvoice.credit_notes import permitted_for_booking
    except ImportError:
        return False
    return permitted_for_booking(booking)


def _record(booking, user, *, origin, destination, q: RefundQuote, account=None, txn=None, actor=None,
            reason: str = "", account_confirmed: bool = False, starts_at=None) -> ExternalRefund:
    snapshot = account_dict(account, full=True) if account is not None else {}
    if destination != RefundDestination.BANK:
        status = RefundStatus.CLOSED
    elif account is None:
        status = RefundStatus.AWAITING_ACCOUNT
    else:
        if not account_confirmed:
            raise RefundPolicyError("Please review the bank account details and confirm that they are correct.",
                                    code="confirmation_required")
        status = RefundStatus.QUEUED
    try:
        with transaction.atomic():
            refund = ExternalRefund.objects.create(
                booking=booking, user=user, origin=origin, destination=destination, tier=q.tier,
                penalty_percent=q.penalty_percent, paid_amount=q.paid, gst_paid=q.gst_paid,
                return_shipping=q.shipping, refund_base=q.refund_base, refund_gst=q.refund_gst,
                refund_amount=q.refund, gst_refunded=q.gst_refunded, bank_account=account,
                shipping_refunded=q.shipping_refunded, portion_percent=q.portion_percent, reason=reason,
                bank_snapshot=snapshot, wallet_transaction=txn, status=status, created_by=actor or user,
                closed_at=timezone.now() if status == RefundStatus.CLOSED else None,
                account_confirmed_at=timezone.now() if status == RefundStatus.QUEUED else None,
                booking_starts_at=starts_at,
            )
    except IntegrityError as exc:
        raise RefundPolicyError("This booking has already been refunded.", status=409, code="already_refunded") from exc
    if q.gst_refunded and invoice_requested(booking):
        # Only reached when a credit note was allowed (see quote): issue it once the refund is committed.
        from iic_booking.gst_einvoice.credit_notes import on_refund_recorded

        transaction.on_commit(lambda: on_refund_recorded(refund.pk))
    return refund


def _refuse_settled(booking) -> None:
    """A booking settled by the OIC / Main Administrator during a maintenance hold is never refunded."""
    from iic_booking.equipment.maintenance_settlement import refund_lock_reason

    locked = refund_lock_reason(booking)
    if locked:
        raise RefundPolicyError(locked, code="refund_locked")


def cancel(user, booking_id, *, destination: str = "wallet", bank_account_id=None, notes: str = "",
           expected_refund=None, account_confirmed: bool = False) -> dict:
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
        _refuse_settled(booking)
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
            raise RefundPolicyError("There is nothing to refund to a bank account for this booking.",
                                    code="bank_not_allowed")
        if expected_refund is not None and _money(expected_refund) != _money(info["refund"]):
            raise RefundPolicyError("The refund amount has changed since it was shown. Please check it again.",
                                    status=409, code="quote_changed", extra={"quote": info})
        account = None
        if destination == RefundDestination.BANK:
            account = RefundBankAccount.objects.filter(pk=bank_account_id, user=user, is_active=True).first()
            if account is None:
                raise RefundPolicyError("Choose or add the bank account for the refund.", code="account_required")
            if not account_confirmed:
                raise RefundPolicyError("Please review the bank account details and confirm that they are correct.",
                                        code="confirmation_required")
        q = quote(booking, penalty_percent=int(info["penalty_percent"]), tier=info["tier"],
                  include_shipping=info["tier"] == "disruption")
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
        starts_at = first_slot_start(booking)
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
                         destination=destination, q=q, account=account, txn=txn, account_confirmed=account_confirmed,
                         starts_at=starts_at)
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
            from .bank_refunds import _notify_staff_confirmed, schedule_send

            _notify_staff_confirmed(refund)
            schedule_send(refund)
    return {"refund": refund_dict(refund), "booking_status": booking.status}


LAB_FAILURE_KINDS = {
    "equipment_failure": "Equipment failure",
    "analysis_unsuccessful": "Analysis not possible (laboratory or equipment issue)",
}
LAB_FAILURE_BLOCKED = ("CANCELLED", "REFUNDED", "BOOKING_NOT_UTILIZED")


def may_refund_lab_failure(actor, booking) -> bool:
    """The equipment's Officer In Charge, the department's Accounts In Charge, or the Main Administrator."""
    from iic_booking.equipment.models import EquipmentManager

    from .invoices import staff_may_handle

    if staff_may_handle(actor, booking):
        return True
    return EquipmentManager.objects.filter(equipment_id=booking.equipment_id, manager_id=actor.pk,
                                           manager__is_active=True).exists()


def _lab_failure_booking(actor, booking_id, *, lock: bool):
    from iic_booking.equipment.models import Booking

    qs = Booking.objects.select_related("equipment", "user").filter(pk=booking_id)
    booking = (qs.select_for_update(of=("self",)) if lock else qs).first()
    if booking is None:
        raise RefundPolicyError("Booking not found.", status=404, code="not_found")
    if not may_refund_lab_failure(actor, booking):
        raise RefundPolicyError("Only the equipment's Officer In Charge, the Accounts In Charge or the Main "
                                "Administrator can refund a lab failure.", status=403, code="forbidden")
    if not policy_applies(booking.user):
        raise RefundPolicyError("This refund is available for external bookings under the refund policy.",
                                status=403, code="not_available")
    if booking.status in LAB_FAILURE_BLOCKED:
        raise RefundPolicyError("This booking is already cancelled or refunded.", code="not_active")
    if ExternalRefund.objects.filter(booking=booking).exists():
        raise RefundPolicyError("This booking has already been refunded.", status=409, code="already_refunded")
    return booking


def _portion(value) -> int:
    try:
        portion = int(str(value if value not in (None, "") else 100))
    except (TypeError, ValueError):
        raise RefundPolicyError("Enter the share of the service charges to refund (1 to 100%).", code="invalid_portion")
    if not 1 <= portion <= 100:
        raise RefundPolicyError("Enter the share of the service charges to refund (1 to 100%).", code="invalid_portion")
    return portion


def lab_failure_quote(actor, booking_id, *, portion_percent=100) -> dict:
    """What a lab-failure refund would give back (shown to the OIC / Accounts In Charge before confirming). A full
    refund (100%) also returns the return shipping; a partial one refunds that share of the service charges only."""
    booking = _lab_failure_booking(actor, booking_id, lock=False)
    portion = _portion(portion_percent)
    q = quote(booking, penalty_percent=0, tier="lab_failure", include_shipping=portion == 100, portion_percent=portion)
    return {**q.as_dict(), "invoice_requested": not q.gst_refunded, "kinds": LAB_FAILURE_KINDS}


def refund_lab_failure(actor, booking_id, *, kind: str, reason: str, portion_percent=100,
                       expected_refund=None, destination: str = "wallet") -> dict:
    """Refund an external booking the lab could not serve (equipment failure, analysis not possible) to the user's
    wallet. Full (100%): the booking is closed and its remaining slots released; partial: that share of the service
    charges, the booking stays. One refund per booking; amounts as shown (``expected_refund``)."""
    from iic_booking.communication.utils import booking_display_id_for_email
    from iic_booking.equipment.booking_cancellation import perform_booking_cancellation
    from iic_booking.equipment.booking_events import create_booking_event
    from iic_booking.equipment.models import BookingEventType, BookingStatus
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    kind = str(kind or "").strip()
    if kind not in LAB_FAILURE_KINDS:
        raise RefundPolicyError("Choose what went wrong: equipment failure or analysis not possible.", code="invalid_kind")
    reason = " ".join(str(reason or "").split())[:2000]
    if len(reason) < 10:
        raise RefundPolicyError("Describe what happened (at least 10 characters); the user sees it.", code="reason_required")
    portion = _portion(portion_percent)
    destination = str(destination or "wallet").strip().lower()
    if destination not in (RefundDestination.WALLET, RefundDestination.BANK):
        raise RefundPolicyError("Choose where the refund goes.", code="invalid_destination")
    with transaction.atomic():
        booking = _lab_failure_booking(actor, booking_id, lock=True)
        _refuse_settled(booking)
        q = quote(booking, penalty_percent=0, tier="lab_failure", include_shipping=portion == 100,
                  portion_percent=portion)
        if expected_refund is not None and _money(expected_refund) != _money(q.refund):
            raise RefundPolicyError("The refund amount has changed since it was shown. Please check it again.",
                                    status=409, code="quote_changed", extra={"quote": q.as_dict()})
        if q.refund <= 0:
            raise RefundPolicyError("Nothing was paid for this booking, so there is nothing to refund.", code="nothing_paid")
        ref = booking_display_id_for_email(booking)
        eq_name = getattr(booking.equipment, "name", "")
        full_reason = f"{LAB_FAILURE_KINDS[kind]}: {reason}"
        previous = booking.status
        starts_at = first_slot_start(booking)
        if portion == 100:
            perform_booking_cancellation(
                booking, slot_ids=list(booking.daily_slots.values_list("id", flat=True)), should_refund=False,
                cancel_notes=f"Refunded in full by the lab. {full_reason}", actor=actor, allow_started_slots=True,
                cancelled_by_label="admin",
            )
        txn = None
        if destination == RefundDestination.WALLET:
            target, _ = WalletRepository.get_booking_wallet_target(booking.user, getattr(booking.equipment, "internal_department", None))
            if target is None:
                raise RefundPolicyError("No wallet was found for this department to refund to.", status=409, code="no_wallet")
            txn = target.credit(
                amount=q.refund,
                description=f"Refund for Booking {ref}- {eq_name} ({_refund_label(q)})",
                related_user=booking.user,
            )
        refund = _record(booking, booking.user, origin=RefundOrigin.LAB_FAILURE, destination=destination,
                         q=q, txn=txn, actor=actor, reason=full_reason, starts_at=starts_at)
        booking.refresh_from_db()
        if portion == 100 and destination == RefundDestination.WALLET:
            booking.status = BookingStatus.REFUNDED
            booking.save(update_fields=["status"])
        if destination == RefundDestination.BANK:
            message = (f"₹{q.refund} for booking {ref} will be paid to your bank account through SRIC "
                       f"({_refund_label(q)}). Please give and confirm your bank account on the portal so that the "
                       f"request can be sent. Reason: {full_reason}")
        else:
            message = f"{_user_message(q, RefundDestination.WALLET, ref)} Reason: {full_reason}"
        create_booking_event(
            booking=booking, event_type=BookingEventType.REFUNDED if txn else BookingEventType.CANCELLED,
            created_by=actor, previous_status=previous, new_status=booking.status, comment=message,
            metadata={"external_refund_id": refund.pk, **q.as_dict(), "destination": destination, "reason": full_reason},
            send_notification=False,
        )
        notify(f"refund:{refund.pk}:created", [booking.user],
               title=f"Refund for booking {ref}" + (": bank account needed" if destination == RefundDestination.BANK else ""),
               message=message, link=f"/my-refunds?refund={refund.pk}")
    return {"refund": refund_dict(refund), "booking_status": booking.status}


def _refund_label(q: RefundQuote) -> str:
    if q.tier == "lab_failure":
        part = "" if q.portion_percent == 100 else f", {q.portion_percent}% of the service charges"
        return f"equipment failure / analysis not possible{part}"
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
    if q.shipping_refunded > 0:
        ship = f" It includes the return shipping charge (₹{q.shipping_refunded})."
    elif q.shipping > 0:
        ship = f" The return shipping charge (₹{q.shipping}) is not refundable."
    else:
        ship = ""
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
        "shipping_refunded": str(r.shipping_refunded), "portion_percent": r.portion_percent, "reason": r.reason,
        "origin_label": r.get_origin_display(),
        "refund_base": str(r.refund_base), "refund_gst": str(r.refund_gst), "refund_amount": str(r.refund_amount),
        "gst_refunded": r.gst_refunded, "status": r.status, "status_label": r.get_status_display(),
        "created_at": r.created_at.isoformat(), "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None, "close_note": r.close_note,
        "bank": ({**r.bank_snapshot, "account_number": masked(r.bank_snapshot.get("account_number", ""))}
                 if r.bank_snapshot and not staff else (r.bank_snapshot or None)),
        "account_confirmed_at": r.account_confirmed_at.isoformat() if r.account_confirmed_at else None,
        "settled_amount": str(r.settled_amount) if r.settled_amount is not None else None,
        "settled_on": r.settled_on.isoformat() if r.settled_on else None,
        "settlement_mode": r.settlement_mode, "settlement_mode_label": r.get_settlement_mode_display(),
        "utr": r.utr if r.status == RefundStatus.CLOSED else "",
        "needs_account": r.status == RefundStatus.AWAITING_ACCOUNT,
    }
    from .bank_refunds import deductions, entitlement

    out["deductions"] = deductions(r)
    out["entitlement"] = entitlement(r)
    if staff:
        from iic_booking.users.display import get_user_display_name

        out["user"] = {"id": r.user_id, "name": get_user_display_name(r.user), "email": r.user.email}
        out["equipment"] = getattr(r.booking.equipment, "name", "")
        dept = getattr(r.booking.equipment, "internal_department", None)
        out["department"] = getattr(dept, "name", "") or ""
        out["utr"] = r.utr
        out["attempts"], out["last_error"], out["sent_to"] = r.attempts, r.last_error, r.sent_to
    return out


def my_refunds(user) -> list[dict]:
    qs = ExternalRefund.objects.filter(user=user).select_related("booking").order_by("-created_at")
    return [refund_dict(r) for r in qs[:100]]


def staff_may_track(user) -> bool:
    from .bank_refunds import may_handle

    return may_handle(user)


def tracker(user, *, status: str = "", q: str = "") -> list[dict]:
    if not staff_may_track(user):
        raise RefundPolicyError("Only the Accounts In Charge can see refunds.", status=403, code="forbidden")
    from django.db.models import Q as _Q

    from iic_booking.department_modules.access import is_main_admin

    qs = ExternalRefund.objects.filter(destination=RefundDestination.BANK).select_related(
        "booking__equipment__internal_department", "user")
    if not is_main_admin(user):
        qs = qs.filter(_Q(booking__equipment__internal_department__isnull=True)
                       | _Q(booking__equipment__internal_department_id=getattr(user, "department_id", None)))
    if status == "open":
        qs = qs.exclude(status=RefundStatus.CLOSED)
    elif status:
        qs = qs.filter(status=status)
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(booking__virtual_booking_id__icontains=q) | Q(user__email__icontains=q) | Q(utr__icontains=q))
    return [refund_dict(r, staff=True) for r in qs.order_by("status", "-created_at")[:300]]


def close(user, refund_id, *, utr: str = "", note: str = "", amount=None, settled_on=None,
          mode: str = "") -> ExternalRefund:
    """Record the settlement: SRIC has paid the money into the user's account (see ``bank_refunds.settle``)."""
    from .bank_refunds import settle

    return settle(user, refund_id, amount=amount, settled_on=settled_on, mode=mode, utr=utr, note=note)
