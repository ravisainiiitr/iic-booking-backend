"""Charge for IIC material used on an own-material fabrication booking.

When a 3D print or 2D laser cutting booking was made with "I will bring my own printing / sheet material" and the
user's material was not enough, the equipment's Officer In Charge (or an active substitute OIC) or the Main
Administrator charges the IIC material that was used.

Pricing matches a normal booking: quantity × master-list price (per sheet or per gram), rounded to the rupee,
plus external GST on that amount for external users; a Discounted Charge Profile waives it. The charge is posted
through the post-booking charge adjustment: the booking total goes up, the pending extra amount goes up, and the
same wallet debit as Deduct Money collects it at once. When the wallet cannot pay, it stays as the extra amount to
pay (Pay Now / Deduct Money). A reversal lowers the total again; money already collected becomes a pending refund
that the Officer In Charge confirms.

Active charges are added back by every recalculation (see ``add_material_charges``).
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .calculators import _format_rate, quantize_money
from .fabrication_material_support import bookable_materials
from .models import (
    Booking,
    BookingEventType,
    BookingMaterialCharge,
    BookingStatus,
    ChargeProfilePricingProfile,
    EquipmentProfileType,
    LaserSheetMaterial,
    PrintMaterial,
)

logger = logging.getLogger(__name__)

MATERIAL_LINE_KEY = "material_charge_id"
UNIT_SHEET = "sheet"
UNIT_GRAM = "g"
MAX_SHEETS = Decimal("1000")
MAX_GRAMS = Decimal("1000000")
MAX_REASON_LENGTH = 1000
ELIGIBLE_STATUSES = (BookingStatus.BOOKED, BookingStatus.PROCESSING, BookingStatus.COMPLETED)
FABRICATION_PROFILES = (EquipmentProfileType.PRINT_3D, EquipmentProfileType.LASER_CUT_2D)
PERMISSION_MESSAGE = (
    "Only the Officer In Charge of this equipment (including an active substitute OIC) and the Main "
    "Administrator can charge for IIC material."
)


class MaterialChargeError(Exception):
    def __init__(self, message: str, http_status: int = status.HTTP_400_BAD_REQUEST, code: str = ""):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code


# --------------------------------------------------------------------------- totals and breakdown lines


def active_material_charges(booking):
    if not getattr(booking, "pk", None):
        return BookingMaterialCharge.objects.none()
    return BookingMaterialCharge.objects.filter(booking_id=booking.pk, reversed_at__isnull=True).order_by(
        "created_at", "pk"
    )


def active_material_charges_total(booking) -> Decimal:
    total = active_material_charges(booking).aggregate(s=Sum("amount"))["s"]
    return Decimal(total or 0).quantize(Decimal("0.01"))


def format_quantity(quantity) -> str:
    q = Decimal(str(quantity)).normalize()
    return f"{q:f}"


def unit_label(unit: str, quantity) -> str:
    if unit == UNIT_SHEET:
        return "sheet" if Decimal(str(quantity)) <= 1 else "sheets"
    return "g"


def quantity_label(charge) -> str:
    qty = format_quantity(charge.quantity)
    if charge.unit == UNIT_GRAM:
        return f"{qty} g"
    return f"{qty} {unit_label(charge.unit, charge.quantity)}"


def material_charge_description(charge) -> str:
    text = f"IIC material used: {charge.material_name} × {quantity_label(charge)}"
    if charge.gst_amount and charge.gst_amount > 0 and not charge.amount_overridden:
        text += f" (incl. GST {format_quantity(charge.gst_percent)}%)"
    return text


def material_charge_line(charge) -> dict:
    return {
        "description": material_charge_description(charge),
        "amount": float(charge.amount),
        MATERIAL_LINE_KEY: charge.pk,
    }


def _without_material_lines(breakdown) -> list:
    return [
        line for line in (breakdown or []) if not (isinstance(line, dict) and line.get(MATERIAL_LINE_KEY) is not None)
    ]


def add_material_charges(booking, total, breakdown):
    """Add the booking's active IIC material charges to a freshly calculated total and breakdown."""
    lines = _without_material_lines(breakdown)
    charges = list(active_material_charges(booking))
    if not charges:
        return total, lines
    extra = sum((c.amount for c in charges), Decimal("0.00"))
    return Decimal(str(total)) + extra, lines + [material_charge_line(c) for c in charges]


def _rebuilt_breakdown(booking) -> list:
    lines = _without_material_lines(booking.charge_breakdown)
    return lines + [material_charge_line(c) for c in active_material_charges(booking)]


# --------------------------------------------------------------------------- permissions and eligibility


def user_can_charge_material(user, equipment) -> bool:
    from .api_views import _user_can_act_as_oic_for_equipment

    return _user_can_act_as_oic_for_equipment(user, equipment)


def user_can_override_amount(user) -> bool:
    from .api_views import _is_admin_user

    return _is_admin_user(user)


def booking_user_type(booking) -> str:
    return (getattr(booking, "user_type_snapshot", None) or getattr(booking.user, "user_type", None) or "").strip()


def _status_reason(booking) -> str | None:
    if booking.status in ELIGIBLE_STATUSES:
        return None
    label = dict(BookingStatus.choices).get(booking.status, booking.status)
    if booking.status == BookingStatus.CANCELLED:
        return "This booking was cancelled, so IIC material can no longer be charged."
    if booking.status == BookingStatus.REFUNDED:
        return "This booking was refunded, so IIC material can no longer be charged."
    return (
        f"This booking is '{label}'. IIC material can be charged only on Booked, Processing results or "
        "Completed bookings."
    )


def _payment_window_reason(booking) -> str | None:
    from .input_edit_payment_window import expire_unpaid_input_edit, has_payment_window

    expire_unpaid_input_edit(booking)
    if has_payment_window(booking):
        return (
            "The user has an unpaid edit with a payment countdown on this booking. Try again once it is paid or "
            "cancelled."
        )
    return None


def charge_ineligible_reason(booking, *, for_reversal: bool = False) -> str | None:
    """Why IIC material cannot be charged (or a charge reversed) on this booking now; None when it can."""
    equipment = booking.equipment
    if getattr(equipment, "profile_type", None) not in FABRICATION_PROFILES:
        return "IIC material charges apply only to 3D printing and 2D laser cutting bookings."
    if not for_reversal and not booking.own_material:
        return (
            "The user did not bring their own material for this booking, so the material is already part of the "
            "booking charge."
        )
    if booking.source_booking_id is not None:
        return "Charge adjustments do not apply to repeat sample bookings."
    return _status_reason(booking) or _payment_window_reason(booking)


# --------------------------------------------------------------------------- materials and pricing


def materials_for_booking(booking):
    """Supported, enabled master-list materials of the booking's equipment, for the booking user's type."""
    materials = bookable_materials(booking.equipment)
    user_type = booking_user_type(booking)
    typed = materials.filter(user_type=user_type)
    if user_type and typed.exists():
        return typed
    return (materials.filter(user_type__isnull=True) | materials.filter(user_type="")).distinct()


def _unit_for(equipment) -> str:
    return UNIT_SHEET if equipment.profile_type == EquipmentProfileType.LASER_CUT_2D else UNIT_GRAM


def _unit_price(material) -> Decimal:
    if isinstance(material, LaserSheetMaterial):
        return Decimal(material.sheet_rate)
    return Decimal(material.price_per_gram)


def material_option(material) -> dict:
    row = {
        "id": material.pk,
        "code": material.code,
        "name": material.name,
        "unit_price": _format_rate(_unit_price(material)),
    }
    if isinstance(material, LaserSheetMaterial):
        row.update(
            {
                "thickness_mm": str(material.thickness_mm),
                "sheet_width_mm": str(material.sheet_width_mm),
                "sheet_height_mm": str(material.sheet_height_mm),
            }
        )
    return row


def booking_gst_percent(booking) -> Decimal:
    from .api_views import get_external_gst_percent

    if not UserType.is_external_user(booking_user_type(booking)):
        return Decimal("0")
    try:
        pct = Decimal(str(get_external_gst_percent()))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    return pct if pct > 0 else Decimal("0")


def _rupees(value) -> Decimal:
    """Whole rupees, like booking totals, kept with two decimals."""
    return quantize_money(value).quantize(Decimal("0.01"))


def _is_discounted(booking) -> bool:
    profile = getattr(booking, "charge_profile", None)
    return getattr(profile, "pricing_profile", None) == ChargeProfilePricingProfile.DISCOUNTED


def _parse_decimal(raw, label: str) -> Decimal:
    if raw is None or str(raw).strip() == "":
        raise MaterialChargeError(f"Enter the {label}.")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise MaterialChargeError(f"The {label} must be a number.")
    if not value.is_finite():
        raise MaterialChargeError(f"The {label} must be a number.")
    return value


def parse_quantity(equipment, raw) -> Decimal:
    if _unit_for(equipment) == UNIT_SHEET:
        qty = _parse_decimal(raw, "number of sheets")
        if qty <= 0:
            raise MaterialChargeError("The number of sheets must be more than 0.")
        if qty > MAX_SHEETS:
            raise MaterialChargeError(f"The number of sheets cannot be more than {MAX_SHEETS}.")
        if qty != qty.quantize(Decimal("0.01")):
            raise MaterialChargeError("Enter the sheets with at most 2 decimals (for example 0.25 for a quarter sheet).")
        return qty.quantize(Decimal("0.01"))
    qty = _parse_decimal(raw, "weight in grams")
    if qty <= 0:
        raise MaterialChargeError("The weight must be more than 0 g.")
    if qty > MAX_GRAMS:
        raise MaterialChargeError(f"The weight cannot be more than {MAX_GRAMS} g.")
    # Billed in whole grams, like the booking estimate and the actual weight.
    return Decimal(int(math.ceil(qty)))


def resolve_material(booking, material_id):
    model = LaserSheetMaterial if booking.equipment.profile_type == EquipmentProfileType.LASER_CUT_2D else PrintMaterial
    if material_id in (None, ""):
        raise MaterialChargeError("Choose the IIC material that was used.")
    try:
        pk = int(material_id)
    except (TypeError, ValueError):
        raise MaterialChargeError("Choose the IIC material that was used.")
    material = materials_for_booking(booking).filter(pk=pk).first()
    if material is None or not isinstance(material, model):
        raise MaterialChargeError(
            "This material is not a supported, enabled material of this equipment for the booking's user type."
        )
    return material


def compute_material_charge(booking, material, quantity: Decimal, *, override_raw=None, actor=None) -> dict:
    """Amount for ``quantity`` of ``material`` on ``booking``, priced like a normal booking."""
    unit = _unit_for(booking.equipment)
    unit_price = _unit_price(material)
    discounted = _is_discounted(booking)
    raw_cost = Decimal("0.00") if discounted else (quantity * unit_price)
    base = _rupees(raw_cost)
    gst_percent = booking_gst_percent(booking)
    gst_amount = _rupees(base * gst_percent / Decimal("100")) if gst_percent > 0 else Decimal("0.00")
    computed = _rupees(base + gst_amount)

    overridden = False
    amount = computed
    if override_raw not in (None, ""):
        if actor is None or not user_can_override_amount(actor):
            raise MaterialChargeError(
                "Only the Main Administrator can enter a different amount.", status.HTTP_403_FORBIDDEN
            )
        override = _parse_decimal(override_raw, "amount")
        if override <= 0:
            raise MaterialChargeError("The amount must be more than ₹0.")
        override = _rupees(override)
        overridden = override != computed
        amount = override

    return {
        "material": material,
        "material_id": material.pk,
        "material_code": material.code,
        "material_name": material.name,
        "unit": unit,
        "quantity": quantity,
        "unit_price": unit_price,
        "raw_cost": raw_cost.quantize(Decimal("0.01")),
        "base_amount": base,
        "gst_percent": gst_percent,
        "gst_amount": gst_amount,
        "computed_amount": computed,
        "amount": amount,
        "amount_overridden": overridden,
        "discounted_profile": discounted,
    }


def _priced_payload(priced: dict) -> dict:
    qty = priced["quantity"]
    return {
        "material_id": priced["material_id"],
        "material_code": priced["material_code"],
        "material_name": priced["material_name"],
        "quantity": format_quantity(qty),
        "unit": priced["unit"],
        "unit_label": unit_label(priced["unit"], qty),
        "unit_price": _format_rate(priced["unit_price"]),
        "material_cost": str(priced["raw_cost"]),
        "base_amount": str(priced["base_amount"]),
        "gst_percent": format_quantity(priced["gst_percent"]),
        "gst_amount": str(priced["gst_amount"]),
        "computed_amount": str(priced["computed_amount"]),
        "amount": str(priced["amount"]),
        "amount_overridden": priced["amount_overridden"],
        "discounted_profile": priced["discounted_profile"],
    }


# --------------------------------------------------------------------------- wallet


def _wallet_target(booking):
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    wallet_target, has_wallet = WalletRepository.get_booking_wallet_target(
        booking.user, getattr(booking.equipment, "internal_department", None)
    )
    return wallet_target if has_wallet else None


def collection_preview(booking, amount: Decimal) -> dict:
    """What happens to ``amount`` on confirm: deducted now, left to pay, or set against a pending refund."""
    from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

    pending_before = booking.charge_recalculation_pending_amount or Decimal("0.00")
    to_collect = (pending_before + amount).quantize(Decimal("0.01"))
    result = {
        "pending_before": str(pending_before),
        "amount_to_collect": str(to_collect) if to_collect > 0 else "0.00",
        "mode": "deduct",
        "message": "",
    }
    if to_collect <= 0:
        result["mode"] = "offset_refund"
        if to_collect < 0:
            result["message"] = (
                f"The pending refund of ₹{abs(pending_before):.2f} goes down to ₹{abs(to_collect):.2f}; nothing is "
                "deducted now."
            )
        else:
            result["message"] = f"This settles the pending refund of ₹{abs(pending_before):.2f}; nothing is deducted now."
        return result
    earlier = (
        f" This includes the earlier unpaid ₹{pending_before:.2f}." if pending_before > 0 else ""
    )
    wallet = _wallet_target(booking)
    if wallet is None:
        result["mode"] = "pay_now"
        result["message"] = (
            f"No wallet is linked to this booking, so ₹{to_collect:.2f} is added to the amount the user has to pay "
            f"(Pay Now).{earlier}"
        )
        return result
    wallet.refresh_from_db()
    ok, _err = subwallet_booking_balance_ok(wallet, to_collect, False)
    if ok:
        result["message"] = f"₹{to_collect:.2f} will be deducted from the booking's wallet now.{earlier}"
    else:
        result["mode"] = "pay_now"
        result["message"] = (
            f"The wallet balance is not enough, so ₹{to_collect:.2f} is added to the amount the user has to pay "
            f"(Pay Now / Deduct Money).{earlier}"
        )
    return result


def _collect_pending_extra(booking, description: str):
    """Debit the pending extra amount like Deduct Money. Returns (transaction, error message)."""
    from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

    from .api_views import _debit_charge_recalculation_extra

    pending = booking.charge_recalculation_pending_amount
    if pending is None or pending <= 0:
        return None, None
    wallet = _wallet_target(booking)
    if wallet is None:
        return None, "No wallet is linked to this booking."
    wallet.refresh_from_db()
    ok, err = subwallet_booking_balance_ok(wallet, pending, False)
    if not ok:
        return None, err or "Insufficient wallet balance."
    try:
        with transaction.atomic():
            txn = _debit_charge_recalculation_extra(booking, wallet, pending, description)
    except ValueError as exc:
        return None, str(exc)
    return txn, None


# --------------------------------------------------------------------------- serialization


def _person(user) -> str:
    if user is None:
        return ""
    return (getattr(user, "name", "") or "").strip() or (getattr(user, "email", "") or "").strip()


def serialize_charge(charge) -> dict:
    return {
        "id": charge.pk,
        "line": material_charge_description(charge),
        "material_code": charge.material_code,
        "material_name": charge.material_name,
        "quantity": format_quantity(charge.quantity),
        "unit": charge.unit,
        "unit_label": unit_label(charge.unit, charge.quantity),
        "unit_price": _format_rate(charge.unit_price),
        "base_amount": str(charge.base_amount),
        "gst_percent": format_quantity(charge.gst_percent),
        "gst_amount": str(charge.gst_amount),
        "computed_amount": str(charge.computed_amount),
        "amount": str(charge.amount),
        "amount_overridden": charge.amount_overridden,
        "reason": charge.reason,
        "created_at": charge.created_at.isoformat() if charge.created_at else None,
        "created_by_name": _person(charge.created_by),
        "deducted_from_wallet": charge.wallet_transaction_id is not None,
        "reversed": charge.reversed_at is not None,
        "reversed_at": charge.reversed_at.isoformat() if charge.reversed_at else None,
        "reversed_by_name": _person(charge.reversed_by),
        "reversal_reason": charge.reversal_reason,
    }


def _charge_metadata(charge) -> dict:
    return {
        "id": charge.pk,
        "material_code": charge.material_code,
        "material_name": charge.material_name,
        "quantity": format_quantity(charge.quantity),
        "unit": charge.unit,
        "unit_price": _format_rate(charge.unit_price),
        "base_amount": str(charge.base_amount),
        "gst_percent": format_quantity(charge.gst_percent),
        "gst_amount": str(charge.gst_amount),
        "computed_amount": str(charge.computed_amount),
        "amount": str(charge.amount),
        "amount_overridden": charge.amount_overridden,
        "reason": charge.reason,
    }


# --------------------------------------------------------------------------- posting and reversal


def _clean_reason(raw, label: str = "reason") -> str:
    text = str(raw or "").strip()
    if not text:
        raise MaterialChargeError(f"Enter the {label}.")
    if len(text) > MAX_REASON_LENGTH:
        raise MaterialChargeError(f"The {label} can be at most {MAX_REASON_LENGTH} characters.")
    return text


def _pending_after(pending_before, delta: Decimal):
    value = ((pending_before or Decimal("0.00")) + delta).quantize(Decimal("0.01"))
    return value if value != 0 else None


def post_material_charge(actor, booking, priced: dict, reason: str):
    """Record the charge, raise the booking total, and collect it like Deduct Money (or leave it to pay)."""
    from iic_booking.communication.utils import booking_display_id_for_email
    from iic_booking.communication.wallet_notifications import send_sub_wallet_transaction_notifications

    from .api_views import _student_booking_description_suffix
    from .booking_events import create_booking_event
    from .dept_admin_actions import record_staff_action

    amount = priced["amount"]
    if amount <= 0:
        raise MaterialChargeError("The amount is ₹0, so there is nothing to charge.")

    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        reason_blocked = charge_ineligible_reason(locked)
        if reason_blocked:
            raise MaterialChargeError(reason_blocked, code="NOT_ELIGIBLE")
        material = priced["material"]
        charge = BookingMaterialCharge.objects.create(
            booking=locked,
            profile_type=locked.equipment.profile_type,
            print_material=material if isinstance(material, PrintMaterial) else None,
            laser_material=material if isinstance(material, LaserSheetMaterial) else None,
            material_code=priced["material_code"],
            material_name=priced["material_name"],
            quantity=priced["quantity"],
            unit=priced["unit"],
            unit_price=priced["unit_price"],
            base_amount=priced["base_amount"],
            gst_percent=priced["gst_percent"],
            gst_amount=priced["gst_amount"],
            computed_amount=priced["computed_amount"],
            amount=amount,
            amount_overridden=priced["amount_overridden"],
            reason=reason,
            created_by=actor,
        )
        previous_charge = Decimal(locked.total_charge or 0).quantize(Decimal("0.01"))
        pending_before = locked.charge_recalculation_pending_amount
        locked.total_charge = previous_charge + amount
        locked.charge_breakdown = _rebuilt_breakdown(locked)
        locked.charge_recalculation_pending_amount = _pending_after(pending_before, amount)
        locked.save(update_fields=["total_charge", "charge_breakdown", "charge_recalculation_pending_amount"])
        pending = locked.charge_recalculation_pending_amount

        equipment = locked.equipment
        line = material_charge_description(charge)
        wallet = _wallet_target(locked)
        description = (
            f"IIC material used ({charge.material_name} × {quantity_label(charge)}) for {equipment.name}, "
            f"Booking {booking_display_id_for_email(locked)}"
        )
        description += _student_booking_description_suffix(wallet, locked.user)
        txn, collect_error = _collect_pending_extra(locked, description)
        if txn is not None:
            charge.wallet_transaction_id = txn.pk
            charge.save(update_fields=["wallet_transaction_id"])

        metadata = {
            "previous_charge": str(previous_charge),
            "new_charge": str(locked.total_charge),
            "charge_breakdown": locked.charge_breakdown,
            "material_charge": _charge_metadata(charge),
        }
        comment = f"{line}: ₹{amount:.2f}. Reason: {reason}."
        if txn is not None:
            metadata["amount_debited"] = str(txn.amount)
            metadata["wallet_transaction_id"] = txn.pk
            comment += f" ₹{txn.amount:.2f} has been deducted from the wallet."
            if pending_before is not None and pending_before > 0:
                comment += f" This includes the earlier unpaid ₹{pending_before:.2f}."
        elif pending is not None and pending > 0:
            metadata["extra_amount"] = str(pending)
            if collect_error:
                metadata["collection_error"] = collect_error
                comment += f" It could not be deducted from the wallet ({collect_error.rstrip('.')})."
            comment += f" Extra ₹{pending:.2f} to pay — click Pay Now to debit wallet."
        elif pending is not None and pending < 0:
            metadata["refund_amount"] = str(abs(pending))
            metadata["refund_status"] = "awaiting_oic_confirmation"
            comment += (
                f" It is set against the pending refund, which is now ₹{abs(pending):.2f} and waits for the "
                "Officer In Charge's approval."
            )
        else:
            comment += " It settles the pending refund, so nothing more is due."

        create_booking_event(
            booking=locked,
            event_type=BookingEventType.CHARGE_RECALCULATED,
            created_by=actor,
            comment=comment,
            metadata=metadata,
            send_notification=True,
        )
        if txn is not None:
            try:
                send_sub_wallet_transaction_notifications(transaction=txn, booking=locked)
            except Exception:
                logger.exception("Wallet debit notification failed for material charge %s", charge.pk)
    record_staff_action(
        actor, "material_charge.create", equipment_id=locked.equipment_id, booking_id=locked.booking_id,
        charge_id=charge.pk,
    )
    summary = {
        "previous_charge": str(previous_charge),
        "new_charge": str(locked.total_charge),
        "amount": str(amount),
        "deducted_amount": str(txn.amount) if txn is not None else None,
        "extra_amount": str(pending) if txn is None and pending is not None and pending > 0 else None,
        "refund_amount": str(abs(pending)) if pending is not None and pending < 0 else None,
        "collection_error": collect_error,
    }
    return locked, charge, summary


def reverse_material_charge(actor, booking, charge_id, reason: str):
    """Reverse a charge made in error. Money already collected becomes a refund the Officer In Charge confirms."""
    from .booking_events import create_booking_event
    from .dept_admin_actions import record_staff_action

    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        reason_blocked = charge_ineligible_reason(locked, for_reversal=True)
        if reason_blocked:
            raise MaterialChargeError(reason_blocked, code="NOT_ELIGIBLE")
        try:
            charge = BookingMaterialCharge.objects.select_for_update().get(pk=int(charge_id), booking_id=locked.pk)
        except (BookingMaterialCharge.DoesNotExist, TypeError, ValueError):
            raise MaterialChargeError("Material charge not found.", status.HTTP_404_NOT_FOUND)
        if charge.reversed_at is not None:
            raise MaterialChargeError("This material charge has already been reversed.")
        charge.reversed_at = timezone.now()
        charge.reversed_by = actor
        charge.reversal_reason = reason
        charge.save(update_fields=["reversed_at", "reversed_by", "reversal_reason"])

        previous_charge = Decimal(locked.total_charge or 0).quantize(Decimal("0.01"))
        locked.total_charge = max(Decimal("0.00"), previous_charge - charge.amount)
        locked.charge_breakdown = _rebuilt_breakdown(locked)
        locked.charge_recalculation_pending_amount = _pending_after(
            locked.charge_recalculation_pending_amount, -charge.amount
        )
        locked.save(update_fields=["total_charge", "charge_breakdown", "charge_recalculation_pending_amount"])
        pending = locked.charge_recalculation_pending_amount

        metadata = {
            "previous_charge": str(previous_charge),
            "new_charge": str(locked.total_charge),
            "charge_breakdown": locked.charge_breakdown,
            "material_charge_reversed": {**_charge_metadata(charge), "reversal_reason": reason},
        }
        comment = (
            f"IIC material charge reversed: {charge.material_name} × {quantity_label(charge)}, "
            f"₹{charge.amount:.2f}. Reason: {reason}."
        )
        if pending is not None and pending < 0:
            metadata["refund_amount"] = str(abs(pending))
            metadata["refund_status"] = "awaiting_oic_confirmation"
            comment += (
                f" Refund of ₹{abs(pending):.2f} is waiting for the Officer In Charge's approval; it will be "
                "credited to the wallet once approved."
            )
        elif pending is not None and pending > 0:
            metadata["extra_amount"] = str(pending)
            comment += f" Extra ₹{pending:.2f} still to pay — click Pay Now to debit wallet."
        else:
            comment += " The unpaid amount for this charge has been removed; nothing more is due."
        create_booking_event(
            booking=locked,
            event_type=BookingEventType.CHARGE_RECALCULATED,
            created_by=actor,
            comment=comment,
            metadata=metadata,
            send_notification=True,
        )
    record_staff_action(
        actor, "material_charge.reverse", equipment_id=locked.equipment_id, booking_id=locked.booking_id,
        charge_id=charge.pk,
    )
    return locked, charge, {
        "previous_charge": str(previous_charge),
        "new_charge": str(locked.total_charge),
        "refund_amount": str(abs(pending)) if pending is not None and pending < 0 else None,
        "extra_amount": str(pending) if pending is not None and pending > 0 else None,
    }


# --------------------------------------------------------------------------- API views


def _load_booking(booking_id):
    return (
        Booking.objects.select_related("equipment", "user", "charge_profile")
        .filter(booking_id=booking_id)
        .first()
    )


def _guard(request, booking_id):
    """(booking, None) or (None, error Response)."""
    booking = _load_booking(booking_id)
    if booking is None:
        return None, Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if not user_can_charge_material(request.user, booking.equipment):
        return None, Response({"error": PERMISSION_MESSAGE}, status=status.HTTP_403_FORBIDDEN)
    return booking, None


def _error(exc: MaterialChargeError) -> Response:
    body = {"error": exc.message}
    if exc.code:
        body["code"] = exc.code
    return Response(body, status=exc.http_status)


def _booking_payload(request, booking):
    from .serializers import BookingSerializer

    booking.refresh_from_db()
    return BookingSerializer(booking, context={"request": request}).data


def _overview(request, booking) -> dict:
    reason = charge_ineligible_reason(booking)
    unit = _unit_for(booking.equipment) if booking.equipment.profile_type in FABRICATION_PROFILES else ""
    charges = BookingMaterialCharge.objects.filter(booking_id=booking.pk).select_related("created_by", "reversed_by")
    return {
        "eligible": reason is None,
        "ineligible_reason": reason,
        "reversal_blocked_reason": charge_ineligible_reason(booking, for_reversal=True),
        "can_override_amount": user_can_override_amount(request.user),
        "profile_type": booking.equipment.profile_type,
        "unit": unit,
        "gst_percent": format_quantity(booking_gst_percent(booking)),
        "discounted_profile": _is_discounted(booking),
        "pending_amount": (
            str(booking.charge_recalculation_pending_amount)
            if booking.charge_recalculation_pending_amount is not None
            else None
        ),
        "materials": [material_option(m) for m in materials_for_booking(booking)] if reason is None else [],
        "charges": [serialize_charge(c) for c in charges],
    }


def _priced_from_request(request, booking) -> dict:
    material = resolve_material(booking, request.data.get("material_id"))
    quantity = parse_quantity(booking.equipment, request.data.get("quantity"))
    return compute_material_charge(
        booking, material, quantity, override_raw=request.data.get("override_amount"), actor=request.user
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_material_charges(request, booking_id):
    """GET: material charges of the booking and what can be charged. POST: charge for IIC material used.

    POST body: ``material_id``, ``quantity`` (sheets for laser cutting, grams for 3D printing), ``reason``,
    optional ``override_amount`` (Main Administrator only).
    """
    booking, denied = _guard(request, booking_id)
    if denied is not None:
        return denied
    if request.method == "GET":
        return Response(_overview(request, booking))

    try:
        blocked = charge_ineligible_reason(booking)
        if blocked:
            raise MaterialChargeError(blocked, code="NOT_ELIGIBLE")
        reason = _clean_reason(request.data.get("reason"))
        priced = _priced_from_request(request, booking)
        booking, charge, summary = post_material_charge(request.user, booking, priced, reason)
    except MaterialChargeError as exc:
        return _error(exc)

    if summary["deducted_amount"]:
        message = f"IIC material charged: ₹{summary['amount']}. ₹{summary['deducted_amount']} deducted from the wallet."
    elif summary["extra_amount"]:
        message = (
            f"IIC material charged: ₹{summary['amount']}. It could not be deducted from the wallet, so "
            f"₹{summary['extra_amount']} is now to be paid (Pay Now / Deduct Money)."
        )
    else:
        message = f"IIC material charged: ₹{summary['amount']}, set against the pending refund."
    return Response(
        {
            "message": message,
            "charge": serialize_charge(charge),
            "summary": summary,
            "booking": _booking_payload(request, booking),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_material_charge_preview(request, booking_id):
    """Price an IIC material charge without saving it (same body as the charge)."""
    booking, denied = _guard(request, booking_id)
    if denied is not None:
        return denied
    try:
        blocked = charge_ineligible_reason(booking)
        if blocked:
            raise MaterialChargeError(blocked, code="NOT_ELIGIBLE")
        priced = _priced_from_request(request, booking)
    except MaterialChargeError as exc:
        return _error(exc)
    payload = _priced_payload(priced)
    amount = priced["amount"]
    payload["can_confirm"] = amount > 0
    payload["collection"] = collection_preview(booking, amount) if amount > 0 else None
    payload["line"] = (
        f"IIC material used: {priced['material_name']} × {format_quantity(priced['quantity'])} "
        f"{unit_label(priced['unit'], priced['quantity'])}"
    )
    if amount <= 0:
        payload["message"] = (
            "This booking uses the Discounted Charge Profile, so a normal booking would not charge for material."
            if priced["discounted_profile"]
            else "The amount is ₹0, so there is nothing to charge."
        )
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reverse_booking_material_charge(request, booking_id, charge_id):
    """Reverse an IIC material charge made in error (body: ``reason``)."""
    booking, denied = _guard(request, booking_id)
    if denied is not None:
        return denied
    try:
        reason = _clean_reason(request.data.get("reason"), "reason for the reversal")
        booking, charge, summary = reverse_material_charge(request.user, booking, charge_id, reason)
    except MaterialChargeError as exc:
        return _error(exc)
    if summary["refund_amount"]:
        message = (
            f"Charge reversed. The refund of ₹{summary['refund_amount']} waits for the Officer In Charge's "
            "Confirm refund."
        )
    elif summary["extra_amount"]:
        message = f"Charge reversed. ₹{summary['extra_amount']} is still to be paid."
    else:
        message = "Charge reversed. The unpaid amount for this charge has been removed."
    return Response(
        {
            "message": message,
            "charge": serialize_charge(charge),
            "summary": summary,
            "booking": _booking_payload(request, booking),
        }
    )
