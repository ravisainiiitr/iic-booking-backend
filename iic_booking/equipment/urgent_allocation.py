"""
Type B urgent requests without slots.

The user submits the booking inputs only; the amount (category rate + 50% urgent surcharge, GST for
external users) is worked out from those inputs with the same charge engine and charge profile
resolution as normal bookings. The Officer in charge later approves the request by choosing slots
on any day (weekends, holidays and maintenance included); the booking is then created as a HOLD and
converted exactly like an approved urgent hold (wallet debit, BOOKED, confirmation emails).
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from iic_booking.communication.email_branding import strftime_slot_end

from .calculators import (
    ChargeCalculationEngine,
    TimeCalculationEngine,
    apply_urgent_booking_surcharge,
    build_safe_input_values_for_charge_calculation,
    quantize_money,
)
from .models import (
    Booking,
    BookingEventType,
    BookingStatus,
    DailySlot,
    Holiday,
    SlotStatus,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    UrgentBookingRequestType,
)
from .slot_allocation import (
    allocated_capacity_covers_analysis,
    slot_tolerance_minutes_for,
    slots_needed_for_analysis_time,
)

logger = logging.getLogger(__name__)

MAX_PREFERRED_SCHEDULE_LENGTH = 1000


class UrgentAllocationError(Exception):
    def __init__(self, message: str, *, code: str = "", extra: Optional[dict] = None, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}
        self.http_status = http_status

    def payload(self) -> dict:
        body = {"error": self.message, **self.extra}
        if self.code:
            body["code"] = self.code
        return body


def _money(value) -> str:
    return f"{quantize_money(Decimal(str(value or 0))):.2f}"


def _slot_duration(equipment) -> int:
    return int(getattr(equipment, "slot_duration_minutes", None) or 60) or 60


# ----------------------------------------------------------------------------- inputs


def clean_request_input_values(equipment, user, raw, *, selected_parameters=None) -> dict:
    """Clean and validate the Step 1 inputs the same way the booking endpoint does."""
    from . import api_views
    from .calculators import SAMPLE_SETS_KEY, normalize_periodic_table_billable_counts
    from .fabrication import is_fabrication_equipment
    from .typed_table import clean_typed_tables

    if is_fabrication_equipment(equipment):
        raise UrgentAllocationError(
            "Type B urgent requests without slots are not available for 3D printing or laser cutting.",
            code="URGENT_TYPE_B_NOT_SUPPORTED",
        )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise UrgentAllocationError("input_values must be a JSON object.")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise UrgentAllocationError("input_values must be an object.")

    values: dict[str, Any] = {}
    for key, value in raw.items():
        cleaned = api_views._clean_single_input_value(value)
        if cleaned is None or cleaned == [] or cleaned is False:
            continue
        values[key] = cleaned
    values = normalize_periodic_table_billable_counts(equipment, values)

    if getattr(equipment, "profile_type", None) == "MULTI_PARAM" and "B" not in values and selected_parameters:
        if isinstance(selected_parameters, list) and selected_parameters:
            values["B"] = str(selected_parameters[0])
        elif isinstance(selected_parameters, str) and selected_parameters.strip():
            values["B"] = selected_parameters.strip()

    error = api_views._validate_dynamic_numeric_input_limits(equipment, values, booking_user=user)
    if not error:
        values, error = api_views._normalize_sample_sets_input(
            equipment, values, raw.get(SAMPLE_SETS_KEY), booking_user=user
        )
    if not error:
        values, table_problem = clean_typed_tables(
            equipment, values, user_type=str(getattr(user, "user_type", "") or "")
        )
        if table_problem:
            error = table_problem["message"]
    if error:
        raise UrgentAllocationError(error)
    if not any(not str(k).startswith("_") for k in values):
        raise UrgentAllocationError(
            "Fill in your requirement (sample details) on the booking page before submitting a Type B request.",
            code="URGENT_INPUTS_REQUIRED",
        )
    return values


# ----------------------------------------------------------------------------- amount


def quote_urgent_charge(equipment, user, input_values, *, total_time_minutes: Optional[int] = None) -> dict:
    """
    Required time and amount for ``input_values`` with the 50% urgent surcharge, using the user's
    charge profile (PI / discounted / standard resolution shared with normal bookings).
    The charge uses ``total_time_minutes`` when given, else the required time from the duration formula.
    """
    from .waitlist_booking import _get_external_gst_percent, _resolve_charge_profile_for_user

    charge_profile, user_type, is_external = _resolve_charge_profile_for_user(equipment, user)
    if not charge_profile:
        raise UrgentAllocationError(
            f"No active charge profile for this equipment and user type {user_type}.",
            code="NO_CHARGE_PROFILE",
        )
    safe_inputs = build_safe_input_values_for_charge_calculation(input_values or {}, equipment=equipment)
    slot_duration = _slot_duration(equipment)
    try:
        required = int(
            TimeCalculationEngine.calculate_time(charge_profile, safe_inputs, slot_duration_minutes=slot_duration)
        )
    except Exception as e:
        raise UrgentAllocationError(f"Error calculating time: {e}")
    if required <= 0:
        raise UrgentAllocationError("The inputs give no analysis time. Check the sample details.")
    minutes = int(total_time_minutes) if total_time_minutes is not None else required
    try:
        base, breakdown = ChargeCalculationEngine.calculate_charge(
            charge_profile, safe_inputs, minutes, selected_parameters=None
        )
    except Exception as e:
        raise UrgentAllocationError(f"Error calculating charge: {e}")
    total, breakdown, surcharge = apply_urgent_booking_surcharge(quantize_money(base), breakdown)
    gst_percent = Decimal("0")
    gst_amount = Decimal("0.00")
    if is_external:
        gst_percent = _get_external_gst_percent()
        if gst_percent > 0:
            gst_amount = quantize_money(total * gst_percent / Decimal("100"))
            total = quantize_money(total + gst_amount)
            breakdown = list(breakdown) + [{"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)}]
    tolerance = slot_tolerance_minutes_for(equipment)
    return {
        "charge_profile": charge_profile,
        "user_type": user_type,
        "required_minutes": required,
        "required_slots": slots_needed_for_analysis_time(required, slot_duration, tolerance),
        "slot_duration_minutes": slot_duration,
        "charged_minutes": minutes,
        "total_charge": quantize_money(total),
        "charge_breakdown": list(breakdown),
        "urgent_surcharge_amount": surcharge,
        "gst_percent": float(gst_percent),
        "gst_amount": gst_amount,
    }


def quote_payload(quote: dict) -> dict:
    return {
        "required_minutes": quote["required_minutes"],
        "required_slots": quote["required_slots"],
        "slot_duration_minutes": quote["slot_duration_minutes"],
        "total_charge": _money(quote["total_charge"]),
        "urgent_surcharge_amount": _money(quote["urgent_surcharge_amount"]),
        "gst_percent": quote["gst_percent"],
        "gst_amount": _money(quote["gst_amount"]),
        "charge_breakdown": quote["charge_breakdown"],
    }


def wallet_check(user, equipment, amount) -> dict:
    """Whether the wallet that pays for this user's booking can take ``amount`` now (same rule as approval)."""
    from iic_booking.users.repositories.wallet_repository import WalletRepository
    from iic_booking.users.wallet_credit_facility import (
        wallet_booking_block_message,
        wallet_max_spendable_on_subwallet,
    )

    amount = quantize_money(Decimal(str(amount or 0)))
    target, _ = WalletRepository.get_booking_wallet_target(user, getattr(equipment, "internal_department", None))
    if target is None:
        return {
            "has_wallet": False,
            "available": "0.00",
            "sufficient": amount <= 0,
            "shortfall": _money(amount),
            "message": "" if amount <= 0 else "The user has no wallet for this department.",
        }
    block = wallet_booking_block_message(target)
    spendable = wallet_max_spendable_on_subwallet(target)
    shortfall = max(Decimal("0.00"), amount - spendable)
    sufficient = amount <= 0 or (not block and shortfall <= 0)
    if block and amount > 0:
        message = block
    elif shortfall > 0:
        message = f"Insufficient wallet balance. Required: ₹{amount:.2f}, Available: ₹{spendable:.2f}"
    else:
        message = ""
    return {
        "has_wallet": True,
        "available": _money(spendable),
        "sufficient": sufficient,
        "shortfall": _money(shortfall if amount > 0 else 0),
        "message": message,
    }


def requirement_payload(req: UrgentBookingRequest, *, field_cache: Optional[dict] = None) -> Optional[dict]:
    """What the user asked for on a request without slots: inputs, required time, amount, preferred dates."""
    if not req.requires_slot_allocation:
        return None
    from .input_display import equipment_field_items, input_summary_items

    values = req.input_values if isinstance(req.input_values, dict) else {}
    fields = equipment_field_items(
        req.equipment_id, str(getattr(req.user, "user_type", "") or ""), cache=field_cache
    )
    return {
        "input_values_by_key": values,
        "input_fields": fields,
        "input_summary": input_summary_items(values, fields),
        "required_minutes": req.duration_minutes,
        "required_slots": req.slots_requested,
        "estimated_charge": _money(req.estimated_charge) if req.estimated_charge is not None else None,
        "estimated_charge_breakdown": req.estimated_charge_breakdown or [],
        "preferred_schedule": req.preferred_schedule or "",
    }


# ----------------------------------------------------------------------------- slots


def slot_notes(slot, holidays: dict) -> list[str]:
    notes = []
    local_date = slot.date
    if local_date and local_date.weekday() >= 5:
        notes.append("Weekend")
    if local_date in holidays:
        notes.append(f"Holiday{': ' + holidays[local_date] if holidays[local_date] else ''}")
    if slot.status != SlotStatus.AVAILABLE:
        notes.append(slot.get_status_display())
    return notes


def holidays_for(dates) -> dict:
    return {
        h.date: (h.reason or "").strip()
        for h in Holiday.objects.filter(date__in=list(dates), is_active=True)
    }


def _validate_slots(equipment, slot_ids) -> tuple[list, list[str]]:
    from .mode_utils import family_slots_overlap_conflict

    try:
        ids = sorted({int(x) for x in (slot_ids or [])})
    except (TypeError, ValueError):
        raise UrgentAllocationError("slot_ids must be a list of slot IDs.")
    if not ids:
        raise UrgentAllocationError("Select at least one slot.")
    slots = list(
        DailySlot.objects.filter(slot_master__equipment=equipment, id__in=ids).order_by("start_datetime")
    )
    if len(slots) != len(ids):
        raise UrgentAllocationError("One or more slots were not found for this equipment.")
    taken = [s.id for s in slots if s.booking_id or s.status == SlotStatus.BOOKED]
    if taken:
        raise UrgentAllocationError(f"Slots {taken} are already booked.", code="SLOTS_TAKEN")
    now = timezone.now()
    past = [s.id for s in slots if s.end_datetime and s.end_datetime <= now]
    if past:
        raise UrgentAllocationError("Choose slots that have not ended yet.", code="SLOTS_IN_PAST")
    for s in slots:
        if s.start_datetime and s.end_datetime and family_slots_overlap_conflict(
            equipment, s.start_datetime, s.end_datetime, exclude_slot_ids=ids
        ):
            raise UrgentAllocationError(
                "This instrument is already booked in another mode for an overlapping time.",
                code="SLOTS_TAKEN",
            )
    warnings: list[str] = []
    holidays = holidays_for({s.date for s in slots if s.date})
    flagged = sorted({note for s in slots for note in slot_notes(s, holidays)})
    if flagged:
        warnings.append(
            "Some chosen slots are outside normal booking (" + ", ".join(flagged) + "). "
            "Make sure the operator and lab are available."
        )
    for prev, nxt in zip(slots, slots[1:]):
        if prev.end_datetime and nxt.start_datetime and prev.end_datetime != nxt.start_datetime:
            warnings.append("The chosen slots are not back-to-back.")
            break
    return slots, warnings


def _slot_minutes(slots) -> int:
    return sum(
        int((s.end_datetime - s.start_datetime).total_seconds() / 60)
        for s in slots
        if s.start_datetime and s.end_datetime
    )


def _slot_times(slots) -> list[dict]:
    return [
        {
            "id": s.id,
            "start": s.start_datetime.isoformat() if s.start_datetime else None,
            "end": s.end_datetime.isoformat() if s.end_datetime else None,
        }
        for s in slots
    ]


def _assert_allocatable(urg: UrgentBookingRequest) -> None:
    if urg.status != UrgentBookingRequestStatus.PENDING:
        raise UrgentAllocationError("Only pending requests can be allocated.", code="NOT_PENDING")
    if not urg.requires_slot_allocation or urg.hold_booking_id:
        raise UrgentAllocationError(
            "This request already has held slots. Use Accept instead.", code="NOT_ALLOCATABLE"
        )
    if urg.pending_supervisor_approval:
        raise UrgentAllocationError(
            "The requester's supervisor must approve this surcharge-based urgent request before it can be approved.",
            code="SUPERVISOR_APPROVAL_PENDING",
        )


def quote_allocation(urg: UrgentBookingRequest, slot_ids) -> dict:
    """Amount for the chosen slots (recomputed now) and whether the wallet can pay it."""
    equipment = urg.equipment
    slots, warnings = _validate_slots(equipment, slot_ids)
    slot_minutes = _slot_minutes(slots)
    # Charged for the required time (what the user was shown), like the OIC's manual waitlist confirmation.
    quote = quote_urgent_charge(equipment, urg.user, urg.input_values)
    covers = allocated_capacity_covers_analysis(
        slot_minutes, quote["required_minutes"], slot_tolerance_minutes_for(equipment)
    )
    wallet = wallet_check(urg.user, equipment, quote["total_charge"])
    estimate = urg.estimated_charge
    return {
        "quote": quote,
        "slots": slots,
        "payload": {
            **quote_payload(quote),
            "slot_minutes": slot_minutes,
            "covers_required_time": covers,
            "slot_ids": [s.id for s in slots],
            "slot_times": _slot_times(slots),
            "warnings": warnings,
            "submitted_estimate": _money(estimate) if estimate is not None else None,
            "amount_changed": estimate is not None and quantize_money(estimate) != quote["total_charge"],
            "wallet": wallet,
            "can_allocate": covers and wallet["sufficient"],
        },
    }


# ----------------------------------------------------------------------------- allocate


def _log_overridden_slot_statuses(equipment, slots, actor, request_id: int) -> None:
    """Slot status change log (Disruption history) for slots that were not Available before the allocation."""
    from .disruption_models import DisruptionSource
    from .disruption_service import DisruptionInput, record_slot_status_change

    for s in slots:
        s._disruption_old_status = s.status
    record_slot_status_change(
        equipment,
        slots,
        SlotStatus.BOOKED,
        DisruptionInput(
            user=actor,
            source=DisruptionSource.OTHER,
            label=f"Allocated to Type B urgent request #{request_id}",
        ),
    )


def allocate_urgent_request(urg_id: int, actor, slot_ids, *, admin_notes=None, expected_total=None):
    """
    Approve a Type B request without slots by booking ``slot_ids`` for the requester.
    The charge is worked out again now; ``expected_total`` (the amount the OIC saw) must still match.
    Returns (urg, booking, quote_payload). Raises UrgentAllocationError.
    """
    from . import api_views
    from .booking_events import create_booking_event
    from .models import initial_istem_fbr_fields_for_charge_profile

    with transaction.atomic():
        try:
            urg = (
                UrgentBookingRequest.objects.select_for_update(of=("self",))
                .select_related("user", "equipment", "supervisor", "hold_booking")
                .get(pk=urg_id)
            )
        except UrgentBookingRequest.DoesNotExist:
            raise UrgentAllocationError("Urgent request not found.", http_status=404)
        _assert_allocatable(urg)
        cap_error = api_views._urgent_weekly_cap_error(urg.equipment, urg.request_type, include_pending=False)
        if cap_error:
            raise UrgentAllocationError(cap_error, code="URGENT_WEEKLY_CAP_REACHED")

        result = quote_allocation(urg, slot_ids)
        quote, payload = result["quote"], result["payload"]
        if not payload["covers_required_time"]:
            raise UrgentAllocationError(
                f"The request needs about {quote['required_minutes']} minutes but the chosen slots cover "
                f"{payload['slot_minutes']} minutes. Select more slots.",
                code="SLOTS_TOO_SHORT",
                extra={"quote": payload},
            )
        if expected_total not in (None, ""):
            try:
                expected = quantize_money(Decimal(str(expected_total)))
            except Exception:
                raise UrgentAllocationError("expected_total must be an amount.")
            if expected != quote["total_charge"]:
                raise UrgentAllocationError(
                    f"The amount is now ₹{quote['total_charge']:.2f} (you saw ₹{expected:.2f}). Check it and allocate again.",
                    code="AMOUNT_CHANGED",
                    extra={"quote": payload},
                )
        if not payload["wallet"]["sufficient"]:
            raise UrgentAllocationError(
                payload["wallet"]["message"] or "Insufficient wallet balance.",
                code="INSUFFICIENT_BALANCE",
                extra={"quote": payload},
            )

        ids = payload["slot_ids"]
        locked = list(
            DailySlot.objects.select_for_update()
            .filter(id__in=ids, slot_master__equipment=urg.equipment, booking__isnull=True)
            .exclude(status=SlotStatus.BOOKED)
            .order_by("start_datetime")
        )
        if len(locked) != len(ids):
            raise UrgentAllocationError(
                "One or more slots were just booked by someone else. Choose other slots.", code="SLOTS_TAKEN"
            )

        equipment = urg.equipment
        charge_profile = quote["charge_profile"]
        actor_label = api_views.get_user_display_name(actor) or "the Officer in charge"
        times = ", ".join(
            f"{timezone.localtime(s.start_datetime):%d %b %Y %H:%M}–{strftime_slot_end(timezone.localtime(s.end_datetime), '%H:%M')}"
            for s in locked
            if s.start_datetime and s.end_datetime
        )
        notes = f"Type B urgent request #{urg.id}: slots allocated by {actor_label}."
        if urg.preferred_schedule:
            notes += f"\nUser's preferred dates / time: {urg.preferred_schedule}"
        booking = Booking.objects.create(
            user=urg.user,
            equipment=equipment,
            charge_profile=charge_profile,
            user_type_snapshot=quote["user_type"],
            total_time_minutes=quote["required_minutes"],
            total_charge=quote["total_charge"],
            input_values=urg.input_values or {},
            selected_parameters=None,
            charge_breakdown=quote["charge_breakdown"],
            status=BookingStatus.HOLD,
            notes=notes,
            settlement_department=getattr(equipment, "internal_department", None),
            created_by=actor,
            **initial_istem_fbr_fields_for_charge_profile(charge_profile),
        )
        previous_statuses = {str(s.pk): s.status for s in locked}
        overridden = [s for s in locked if s.status != SlotStatus.AVAILABLE]
        DailySlot.objects.filter(pk__in=[s.pk for s in locked]).update(booking=booking, status=SlotStatus.BOOKED)
        if overridden:
            _log_overridden_slot_statuses(equipment, overridden, actor, urg.id)
        override_note = ""
        if overridden:
            counts: dict[str, int] = {}
            for s in overridden:
                label = s.get_status_display()
                counts[label] = counts.get(label, 0) + 1
            override_note = " Previous slot status: " + ", ".join(f"{k} × {v}" for k, v in sorted(counts.items())) + "."
        create_booking_event(
            booking=booking,
            event_type=BookingEventType.CREATED,
            created_by=actor,
            comment=(
                f"Slots allocated by {actor_label} for Type B urgent request #{urg.id}: {times} "
                f"({quote['required_minutes']} minutes, ₹{quote['total_charge']:.2f} incl. 50% urgent surcharge)."
                + override_note
            ),
            new_status=BookingStatus.HOLD,
            metadata={
                "urgent_booking_request_id": urg.id,
                "urgent_slot_allocation": True,
                "allocated_by_id": getattr(actor, "pk", None),
                "slot_ids": ids,
                "slot_times": payload["slot_times"],
                "slot_warnings": payload["warnings"],
                "previous_slot_statuses": previous_statuses,
            },
            send_notification=False,
        )
        urg.hold_booking = booking
        urg.save(update_fields=["hold_booking"])
        converted, err = api_views._confirm_urgent_hold_booking(urg, actor)
        if err or not converted:
            raise UrgentAllocationError(err or "Could not confirm the booking.", code="CONFIRM_FAILED")
        urg.status = UrgentBookingRequestStatus.APPROVED
        urg.decided_by = actor
        urg.decided_at = timezone.now()
        if admin_notes is not None:
            urg.admin_notes = str(admin_notes)[:4000]
        urg.save(update_fields=["status", "decided_by", "decided_at", "admin_notes"])
    return urg, booking, payload
