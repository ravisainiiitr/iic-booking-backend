"""Shared booking cancellation logic (full and partial slot release)."""

from decimal import Decimal
from typing import Any

from django.utils import timezone

from iic_booking.communication.utils import booking_display_id_for_email
from iic_booking.users.models import UserType
from iic_booking.users.repositories.wallet_repository import WalletRepository

from .booking_events import create_booking_event
from .waitlist import schedule_waitlist_slots_available_after_commit
from .quota_utils import sync_booking_quota_fields_after_partial_cancel
from .calculators import (
    ChargeCalculationEngine,
    TimeCalculationEngine,
    build_safe_input_values_for_charge_calculation,
    safe_decimal,
)
from .maintenance_policy import (
    clear_disruption_policy_fields,
    effective_slot_status_when_freeing_disruption_booking,
    released_slot_status_after_booking_freed,
)
from .models import (
    Booking,
    BookingChargeSetting,
    BookingDisruptionKind,
    BookingEventType,
    BookingStatus,
    ChargeProfilePricingProfile,
    DailySlot,
    EquipmentProfileType,
    PrintAnalysis,
    PrintAnalysisStatus,
)


class CancellationValidationError(Exception):
    """Raised when cancellation request parameters are invalid."""


def user_may_cancel_started_slots(user) -> bool:
    """Officer in charge (manager) and admin may cancel slots that have already started."""
    return getattr(user, "user_type", None) in (UserType.MANAGER, UserType.ADMIN)


def slot_duration_minutes(slot) -> int:
    return int((slot.end_datetime - slot.start_datetime).total_seconds() / 60)


def partial_cancel_uses_input_reduction(profile_type) -> bool:
    """Sample-based profiles require reducing field A (not picking slots) for partial cancel."""
    pt = (profile_type or "").strip().upper()
    return pt in (
        EquipmentProfileType.SAMPLE,
        EquipmentProfileType.SAMPLE_ELEMENT,
        EquipmentProfileType.MULTI_PARAM,
    )


def partial_cancel_reduction_key(profile_type) -> str | None:
    """Input field key the user reduces for partial cancellation (A or B)."""
    pt = (profile_type or "").strip().upper()
    if pt == EquipmentProfileType.HOUR:
        return "B"
    if partial_cancel_uses_input_reduction(pt):
        return "A"
    return None


def _ceil_div(a: int, b: int) -> int:
    return (a + b - 1) // b if b > 0 else 0


def _charge_profile_proxy(booking: Booking):
    cp = booking.charge_profile
    eq = booking.equipment

    class _Proxy:
        def __init__(self, profile, equipment):
            self.equipment = profile.equipment
            self.user_type = profile.user_type
            self.is_active = profile.is_active
            self.primary_unit_charge = profile.primary_unit_charge
            self.secondary_unit_charge = profile.secondary_unit_charge
            self.breakpoint = profile.breakpoint
            self.time_formula = profile.time_formula
            self.pricing_profile = getattr(
                profile, "pricing_profile", ChargeProfilePricingProfile.STANDARD
            )
            self.profile_type = getattr(equipment, "profile_type", None)

    return _Proxy(cp, eq)


def _get_external_gst_percent() -> Decimal:
    try:
        obj = BookingChargeSetting.objects.filter(key="EXTERNAL_GST_PERCENT").first()
        if obj and obj.value:
            return Decimal(obj.value.strip())
    except Exception:
        pass
    return Decimal("18")


def _finalize_charge_for_booking(booking: Booking, base_charge: Decimal, breakdown: list) -> tuple[Decimal, list]:
    """Apply external GST when the booking user is external (matches booking-time rules)."""
    user_type = getattr(booking, "user_type_snapshot", None) or ""
    if not UserType.is_external_user(user_type):
        return base_charge.quantize(Decimal("0.01")), breakdown

    gst_percent = _get_external_gst_percent()
    if gst_percent <= 0:
        return base_charge.quantize(Decimal("0.01")), breakdown

    gst_amount = (base_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
    total = (base_charge + gst_amount).quantize(Decimal("0.01"))
    new_breakdown = list(breakdown) + [
        {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
    ]
    return total, new_breakdown


def _booking_slot_duration_minutes(booking: Booking) -> int:
    eq = booking.equipment
    slot_dur = int(getattr(eq, "slot_duration_minutes", None) or 0)
    if slot_dur > 0:
        return slot_dur
    slots = list(booking.daily_slots.order_by("start_datetime")[:1])
    if slots and slots[0].start_datetime and slots[0].end_datetime:
        return slot_duration_minutes(slots[0])
    return 60


def _merge_reduced_input_values(booking: Booking, reduced_input_values: dict) -> dict:
    if not isinstance(reduced_input_values, dict):
        raise CancellationValidationError("reduced_input_values must be an object.")
    merged = dict(booking.input_values or {})
    for key, value in reduced_input_values.items():
        if not isinstance(key, str) or len(key) != 1 or not key.isalpha():
            raise CancellationValidationError("reduced_input_values keys must be single letter field keys (A-G).")
        merged[key.upper()] = value
    return merged


def compute_partial_cancel_plan(
    booking: Booking,
    *,
    reduced_input_values: dict | None = None,
    slot_ids_to_cancel: list[int] | None = None,
) -> dict[str, Any]:
    """
    Compute slots to release, revised inputs, time, and charge for a partial cancellation.
    Exactly one of reduced_input_values or slot_ids_to_cancel must be provided (partial only).
    """
    all_slots = list(booking.daily_slots.order_by("start_datetime"))
    if not all_slots:
        raise CancellationValidationError("This booking has no slots to cancel.")

    equipment = booking.equipment
    profile_type = getattr(equipment, "profile_type", None)
    slot_dur = _booking_slot_duration_minutes(booking)
    cp_proxy = _charge_profile_proxy(booking)
    previous_charge = Decimal(str(booking.total_charge or "0"))

    if reduced_input_values is not None:
        if not partial_cancel_uses_input_reduction(profile_type):
            raise CancellationValidationError(
                "Input reduction is only supported for sample-based profile types."
            )
        reduction_key = partial_cancel_reduction_key(profile_type)
        merged = _merge_reduced_input_values(booking, reduced_input_values)
        safe_inputs = build_safe_input_values_for_charge_calculation(merged, equipment=equipment)
        current_raw = (booking.input_values or {}).get(reduction_key)
        new_raw = safe_inputs.get(reduction_key)
        try:
            current_val = int(safe_decimal(current_raw, Decimal("0")))
            new_val = int(safe_decimal(new_raw, Decimal("0")))
        except Exception:
            raise CancellationValidationError(f"Field {reduction_key} must be a whole number.")

        if new_val >= current_val:
            raise CancellationValidationError(
                f"Reduced value for field {reduction_key} must be less than the current value ({current_val})."
            )
        if new_val < 1:
            raise CancellationValidationError(
                f"Reduced value for field {reduction_key} must be at least 1. "
                "To cancel the entire booking, release all slots instead."
            )

        try:
            new_time = int(
                TimeCalculationEngine.calculate_time(
                    cp_proxy,
                    safe_inputs,
                    slot_duration_minutes=slot_dur,
                )
            )
        except Exception as e:
            raise CancellationValidationError(f"Could not calculate time for reduced inputs: {e}") from e

        if new_time <= 0:
            raise CancellationValidationError("Reduced inputs require no booking time.")

        slots_needed = _ceil_div(new_time, slot_dur)
        if slots_needed > len(all_slots):
            raise CancellationValidationError(
                "Reduced inputs require more slots than currently booked."
            )

        slots_to_keep = all_slots[:slots_needed]
        slots_to_release = all_slots[slots_needed:]
        keep_time = sum(slot_duration_minutes(s) for s in slots_to_keep)
        new_input_values = dict(booking.input_values or {})
        new_input_values[reduction_key] = new_val
        for k, v in safe_inputs.items():
            if k == reduction_key:
                new_input_values[k] = v

    elif slot_ids_to_cancel is not None:
        if partial_cancel_uses_input_reduction(profile_type):
            raise CancellationValidationError(
                "Partial cancellation for this equipment requires reducing the number of samples "
                "(field A), not selecting slots."
            )
        cancel_set = set(slot_ids_to_cancel)
        all_ids = {s.id for s in all_slots}
        if not cancel_set or not cancel_set.issubset(all_ids):
            raise CancellationValidationError("Invalid slot selection for partial cancellation.")
        if len(cancel_set) >= len(all_slots):
            raise CancellationValidationError("Select fewer than all slots for partial cancellation.")

        slots_to_release = [s for s in all_slots if s.id in cancel_set]
        slots_to_keep = [s for s in all_slots if s.id not in cancel_set]
        keep_time = sum(slot_duration_minutes(s) for s in slots_to_keep)
        new_input_values = dict(booking.input_values or {})

        if (profile_type or "").strip().upper() == EquipmentProfileType.HOUR:
            new_input_values["B"] = len(slots_to_keep)
            safe_inputs = build_safe_input_values_for_charge_calculation(new_input_values, equipment=equipment)
            try:
                new_time = int(
                    TimeCalculationEngine.calculate_time(
                        cp_proxy,
                        safe_inputs,
                        slot_duration_minutes=slot_dur,
                    )
                )
            except Exception:
                new_time = keep_time
        else:
            safe_inputs = build_safe_input_values_for_charge_calculation(new_input_values, equipment=equipment)
            new_time = keep_time
    else:
        raise CancellationValidationError(
            "Provide reduced_input_values or slot_ids for partial cancellation."
        )

    safe_inputs = build_safe_input_values_for_charge_calculation(new_input_values, equipment=equipment)
    try:
        base_charge, breakdown = ChargeCalculationEngine.calculate_charge(
            cp_proxy,
            safe_inputs,
            new_time,
            selected_parameters=getattr(booking, "selected_parameters", None),
        )
        new_charge, new_breakdown = _finalize_charge_for_booking(booking, base_charge, breakdown)
    except Exception as e:
        raise CancellationValidationError(f"Could not calculate charge for revised booking: {e}") from e

    refund_amount = max(Decimal("0.00"), (previous_charge - new_charge).quantize(Decimal("0.01")))

    return {
        "slots_to_keep": slots_to_keep,
        "slots_to_release": slots_to_release,
        "new_input_values": new_input_values,
        "new_time_minutes": new_time,
        "new_charge": new_charge,
        "new_breakdown": new_breakdown,
        "refund_amount": refund_amount,
        "is_full_cancel": len(slots_to_keep) == 0,
    }


def _print_item_input_values(analysis) -> dict[str, Any]:
    from .print_3d_views import get_effective_print_weight_and_time_from_analysis

    weight, time_min = get_effective_print_weight_and_time_from_analysis(analysis)
    material_code = analysis.material_code_snapshot or (
        analysis.material.code if analysis.material else ""
    )
    inputs: dict[str, Any] = {}
    if weight is not None:
        inputs["A"] = int(weight)
    if time_min is not None:
        inputs["C"] = int(time_min)
    if material_code:
        inputs["B"] = material_code
    return inputs


def _calculate_print_charge_for_inputs(
    booking: Booking,
    cp_proxy,
    input_values: dict,
    time_minutes: int,
) -> tuple[Decimal, list]:
    safe_inputs = build_safe_input_values_for_charge_calculation(input_values, equipment=booking.equipment)
    base_charge, breakdown = ChargeCalculationEngine.calculate_charge(
        cp_proxy,
        safe_inputs,
        time_minutes,
        selected_parameters=getattr(booking, "selected_parameters", None),
    )
    return _finalize_charge_for_booking(booking, base_charge, breakdown)


def _sum_print_items_charge(
    booking: Booking,
    cp_proxy,
    items: list,
) -> tuple[Decimal, list]:
    total = Decimal("0.00")
    combined_breakdown: list = []
    for item in items:
        inputs = _print_item_input_values(item)
        time_min = int(inputs.get("C") or 0)
        if time_min <= 0 or not inputs.get("B") or not inputs.get("A"):
            continue
        item_charge, item_breakdown = _calculate_print_charge_for_inputs(
            booking, cp_proxy, inputs, time_min
        )
        total += item_charge
        combined_breakdown.extend(item_breakdown)
    return total.quantize(Decimal("0.01")), combined_breakdown


def compute_partial_cancel_print_items(
    booking: Booking,
    print_analysis_ids_to_cancel: list,
) -> dict[str, Any]:
    """Partial cancellation for 3D print bookings: cancel individual STL files."""
    equipment = booking.equipment
    profile_type = getattr(equipment, "profile_type", None)
    if (profile_type or "").strip().upper() != EquipmentProfileType.PRINT_3D:
        raise CancellationValidationError("Print file cancellation is only for 3D print bookings.")

    all_slots = list(booking.daily_slots.order_by("start_datetime"))
    if not all_slots:
        raise CancellationValidationError("This booking has no slots to cancel.")

    if not print_analysis_ids_to_cancel:
        raise CancellationValidationError("Select at least one print file to cancel.")

    try:
        cancel_ids = {str(x) for x in print_analysis_ids_to_cancel}
    except Exception as exc:
        raise CancellationValidationError("print_analysis_ids must be a list of UUID strings.") from exc

    active_items = list(
        PrintAnalysis.objects.filter(
            booking=booking,
            cancelled_at__isnull=True,
            status=PrintAnalysisStatus.COMPLETED,
        ).select_related("material")
        .order_by("sequence", "created_at")
    )
    if not active_items:
        raise CancellationValidationError("This booking has no active print files.")

    item_map = {str(item.id): item for item in active_items}
    if not cancel_ids.issubset(item_map):
        raise CancellationValidationError("Invalid print file selection for this booking.")
    if len(cancel_ids) >= len(active_items):
        raise CancellationValidationError(
            "To cancel all print files, cancel the entire booking instead."
        )

    remaining = [item for item in active_items if str(item.id) not in cancel_ids]
    cancelled = [item for item in active_items if str(item.id) in cancel_ids]

    total_weight = 0
    total_time = 0
    material_code = ""
    for item in remaining:
        inputs = _print_item_input_values(item)
        total_weight += int(inputs.get("A") or 0)
        total_time += int(inputs.get("C") or 0)
        if not material_code:
            material_code = str(inputs.get("B") or "")

    if total_time <= 0:
        raise CancellationValidationError("At least one print file must remain booked.")

    slot_dur = _booking_slot_duration_minutes(booking)
    cp_proxy = _charge_profile_proxy(booking)
    previous_charge = Decimal(str(booking.total_charge or "0"))

    new_input_values = dict(booking.input_values or {})
    new_input_values["A"] = int(total_weight)
    new_input_values["C"] = total_time
    if material_code:
        new_input_values["B"] = material_code

    slots_needed = _ceil_div(total_time, slot_dur)
    if slots_needed > len(all_slots):
        raise CancellationValidationError(
            "Remaining print files require more slots than currently booked."
        )

    slots_to_keep = all_slots[:slots_needed]
    slots_to_release = all_slots[slots_needed:]

    all_items_charge, _ = _sum_print_items_charge(booking, cp_proxy, active_items)
    new_charge, new_breakdown = _sum_print_items_charge(booking, cp_proxy, remaining)
    cancelled_charge, _ = _sum_print_items_charge(booking, cp_proxy, cancelled)

    if all_items_charge > 0 and previous_charge > 0:
        scale = previous_charge / all_items_charge
        new_charge = (new_charge * scale).quantize(Decimal("0.01"))
        refund_amount = max(
            Decimal("0.00"),
            (previous_charge - new_charge).quantize(Decimal("0.01")),
        )
    elif cancelled_charge > 0:
        refund_amount = min(previous_charge, cancelled_charge).quantize(Decimal("0.01"))
        new_charge = max(Decimal("0.00"), (previous_charge - refund_amount).quantize(Decimal("0.01")))
    else:
        try:
            safe_inputs = build_safe_input_values_for_charge_calculation(new_input_values, equipment=booking.equipment)
            base_charge, breakdown = ChargeCalculationEngine.calculate_charge(
                cp_proxy,
                safe_inputs,
                total_time,
                selected_parameters=getattr(booking, "selected_parameters", None),
            )
            new_charge, new_breakdown = _finalize_charge_for_booking(booking, base_charge, breakdown)
        except Exception as e:
            raise CancellationValidationError(
                f"Could not calculate charge for revised booking: {e}"
            ) from e
        refund_amount = max(Decimal("0.00"), (previous_charge - new_charge).quantize(Decimal("0.01")))

    return {
        "slots_to_keep": slots_to_keep,
        "slots_to_release": slots_to_release,
        "new_input_values": new_input_values,
        "new_time_minutes": total_time,
        "new_charge": new_charge,
        "new_breakdown": new_breakdown,
        "refund_amount": refund_amount,
        "is_full_cancel": len(slots_to_keep) == 0,
        "print_analysis_ids_to_cancel": list(cancel_ids),
    }


def preview_partial_cancellation_print_items(
    booking: Booking,
    *,
    print_analysis_ids: list,
) -> dict[str, Any]:
    plan = compute_partial_cancel_print_items(booking, print_analysis_ids)
    return {
        "refund_amount": str(plan["refund_amount"]),
        "new_charge": str(plan["new_charge"]),
        "new_total_time_minutes": plan["new_time_minutes"],
        "new_input_values": plan["new_input_values"],
        "slots_to_release": [
            {
                "id": s.id,
                "start_datetime": s.start_datetime.isoformat() if s.start_datetime else None,
                "end_datetime": s.end_datetime.isoformat() if s.end_datetime else None,
            }
            for s in plan["slots_to_release"]
        ],
        "slots_to_keep_count": len(plan["slots_to_keep"]),
        "slots_to_release_count": len(plan["slots_to_release"]),
        "print_analysis_ids_to_cancel": plan["print_analysis_ids_to_cancel"],
    }


def preview_partial_cancellation(
    booking: Booking,
    *,
    reduced_input_values: dict | None = None,
    slot_ids_to_cancel: list[int] | None = None,
) -> dict[str, Any]:
    """Preview partial cancellation refund and revised booking fields."""
    plan = compute_partial_cancel_plan(
        booking,
        reduced_input_values=reduced_input_values,
        slot_ids_to_cancel=slot_ids_to_cancel,
    )
    return {
        "refund_amount": str(plan["refund_amount"]),
        "new_charge": str(plan["new_charge"]),
        "new_total_time_minutes": plan["new_time_minutes"],
        "new_input_values": plan["new_input_values"],
        "slots_to_release": [
            {
                "id": s.id,
                "start_datetime": s.start_datetime.isoformat() if s.start_datetime else None,
                "end_datetime": s.end_datetime.isoformat() if s.end_datetime else None,
            }
            for s in plan["slots_to_release"]
        ],
        "slots_to_keep_count": len(plan["slots_to_keep"]),
        "slots_to_release_count": len(plan["slots_to_release"]),
    }


def parse_cancellation_request(request_data, booking) -> dict[str, Any]:
    """
    Parse cancel request body.
    Returns dict with mode 'full_slots', 'partial_slots', or 'partial_reduction'.
    """
    all_ids = list(booking.daily_slots.values_list("id", flat=True))
    if not all_ids:
        raise CancellationValidationError("This booking has no slots to cancel.")

    profile_type = getattr(booking.equipment, "profile_type", None)

    print_analysis_ids = request_data.get("print_analysis_ids")
    if print_analysis_ids is not None:
        if (profile_type or "").strip().upper() != EquipmentProfileType.PRINT_3D:
            raise CancellationValidationError(
                "Print file cancellation is only supported for 3D print bookings."
            )
        if request_data.get("slot_ids") is not None or request_data.get("reduced_input_values") is not None:
            raise CancellationValidationError(
                "Provide only print_analysis_ids for 3D print partial cancellation."
            )
        if not isinstance(print_analysis_ids, list):
            raise CancellationValidationError("print_analysis_ids must be a list of UUID strings.")
        plan = compute_partial_cancel_print_items(booking, print_analysis_ids)
        return {
            "mode": "partial_print_items",
            "slot_ids": [s.id for s in plan["slots_to_release"]],
            "plan": plan,
        }

    reduced = request_data.get("reduced_input_values")
    if reduced is not None:
        if request_data.get("slot_ids") is not None:
            raise CancellationValidationError(
                "Provide either slot_ids or reduced_input_values, not both."
            )
        plan = compute_partial_cancel_plan(booking, reduced_input_values=reduced)
        release_ids = [s.id for s in plan["slots_to_release"]]
        keep_ids = [s.id for s in plan["slots_to_keep"]]
        if not keep_ids:
            return {
                "mode": "full_slots",
                "slot_ids": all_ids,
                "plan": None,
            }
        return {
            "mode": "partial_reduction",
            "slot_ids": release_ids,
            "plan": plan,
        }

    raw_slot_ids = request_data.get("slot_ids")
    if raw_slot_ids is None:
        return {"mode": "full_slots", "slot_ids": all_ids, "plan": None}

    slot_ids = parse_cancellation_slot_ids(request_data, booking)
    if set(slot_ids) == set(all_ids):
        return {"mode": "full_slots", "slot_ids": slot_ids, "plan": None}

    if partial_cancel_uses_input_reduction(profile_type):
        raise CancellationValidationError(
            "Partial cancellation for this equipment requires reducing user inputs "
            "(e.g. number of samples). Use reduced_input_values instead of slot_ids."
        )

    plan = compute_partial_cancel_plan(booking, slot_ids_to_cancel=slot_ids)
    return {
        "mode": "partial_slots",
        "slot_ids": slot_ids,
        "plan": plan,
    }


def parse_cancellation_slot_ids(request_data, booking) -> list[int]:
    all_ids = list(booking.daily_slots.values_list("id", flat=True))
    if not all_ids:
        raise CancellationValidationError("This booking has no slots to cancel.")
    raw = request_data.get("slot_ids")
    if raw is None:
        return all_ids
    if not isinstance(raw, list):
        raise CancellationValidationError("slot_ids must be a list of integers.")
    if len(raw) == 0:
        raise CancellationValidationError("Select at least one slot to cancel.")
    try:
        slot_ids = [int(x) for x in raw]
    except (TypeError, ValueError):
        raise CancellationValidationError("slot_ids must be a list of integers.")
    all_set = set(all_ids)
    if any(sid not in all_set for sid in slot_ids):
        raise CancellationValidationError("One or more selected slots do not belong to this booking.")
    return slot_ids


def validate_slots_for_cancellation(booking, slot_ids, *, allow_started_slots: bool) -> list:
    slots = list(booking.daily_slots.filter(id__in=slot_ids).order_by("start_datetime"))
    if len(slots) != len(set(slot_ids)):
        raise CancellationValidationError("One or more selected slots do not belong to this booking.")
    if not allow_started_slots:
        now = timezone.now()
        for slot in slots:
            if slot.start_datetime <= now:
                raise CancellationValidationError(
                    "Cannot cancel slots that have already started. "
                    "Deselect those slots or contact the Officer in Charge / admin."
                )
    return slots


def calculate_refund_for_cancelled_slots(booking, cancelled_slots) -> Decimal:
    """Equal share of total_charge per booking slot.

    Do **not** use this for intentional partial cancellations when tiered pricing
    (breakpoint / primary / secondary unit charges) may apply. Those must go
    through ``compute_partial_cancel_plan`` / ``ChargeCalculationEngine``.

    Safe uses: full cancellation (n_cancel >= n_all → entire charge) or
    non-priced bookkeeping. ``perform_booking_cancellation`` never uses this
    for partial cancels; it requires or builds a ``partial_plan`` instead.
    """
    all_slots = list(booking.daily_slots.all())
    total_charge = Decimal(str(booking.total_charge or "0"))
    n_all = len(all_slots)
    n_cancel = len(cancelled_slots)
    if n_all == 0 or n_cancel == 0:
        return Decimal("0.00")
    if n_cancel >= n_all:
        return total_charge.quantize(Decimal("0.01"))
    per_slot_share = total_charge / Decimal(n_all)
    return (per_slot_share * Decimal(n_cancel)).quantize(Decimal("0.01"))


def ensure_partial_cancel_plan(
    booking: Booking,
    *,
    slot_ids: list[int],
    partial_plan: dict | None,
) -> dict:
    """
    Guarantee a ChargeCalculationEngine-based plan for any partial cancel.

    API entry points (admin/user cancel) already attach a plan via
    ``parse_cancellation_request``. This exists so direct callers cannot
    silently fall through to equal per-slot refund math.
    """
    if partial_plan is not None:
        return partial_plan
    if not slot_ids:
        raise CancellationValidationError(
            "Partial cancellation requires a computed plan "
            "(reduced_input_values or print_analysis_ids)."
        )
    return compute_partial_cancel_plan(booking, slot_ids_to_cancel=slot_ids)


def perform_booking_cancellation(
    booking: Booking,
    *,
    slot_ids: list[int],
    should_refund: bool,
    cancel_notes: str,
    actor,
    allow_started_slots: bool,
    reverse_reward_points_fn=None,
    student_booking_description_suffix_fn=None,
    cancelled_by_label: str = "user",
    partial_plan: dict | None = None,
) -> dict:
    """
    Release selected slots and optionally refund.

    Full booking cancellation when all slots are selected and no partial_plan.
    Partial cancellation always uses a ``partial_plan`` (recalculated charge) so
    tiered pricing is respected — never equal per-slot refund splits.
    """
    all_slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    is_full_cancel = set(slot_ids) == set(all_slot_ids) and partial_plan is None
    cancelled_slots = (
        validate_slots_for_cancellation(
            booking, slot_ids, allow_started_slots=allow_started_slots
        )
        if slot_ids
        else []
    )

    if not is_full_cancel and (
        booking.status == BookingStatus.DISRUPTION_PENDING
        or getattr(booking, "maintenance_disruption_flag", False)
    ):
        raise CancellationValidationError(
            "Partial cancellation is only available for standard active bookings."
        )

    is_partial = not is_full_cancel and (slot_ids or partial_plan is not None)

    # Call-site audit (admin cancel_booking + user cancel): both pass
    # partial_plan from parse_cancellation_request for every partial path.
    # Maintenance auto-cancel is full-only. Safety net below covers direct calls.
    if is_partial:
        partial_plan = ensure_partial_cancel_plan(
            booking, slot_ids=slot_ids, partial_plan=partial_plan
        )

    previous_status = booking.status

    refund_amount = Decimal("0.00")
    if should_refund:
        if is_partial:
            refund_amount = Decimal(str(partial_plan.get("refund_amount", "0")))
        else:
            # Full cancel: equal-split helper returns the full charge when all slots go.
            refund_amount = calculate_refund_for_cancelled_slots(booking, cancelled_slots)

    free_status = (
        effective_slot_status_when_freeing_disruption_booking(booking)
        if (
            previous_status == BookingStatus.DISRUPTION_PENDING
            or getattr(booking, "maintenance_disruption_flag", False)
        )
        else released_slot_status_after_booking_freed(booking.equipment)
    )

    if slot_ids:
        DailySlot.objects.filter(id__in=slot_ids, booking=booking).update(
            booking=None,
            status=free_status,
        )
        schedule_waitlist_slots_available_after_commit(
            booking.equipment,
            preferred_slot_ids=slot_ids,
            respect_reschedule_threshold=True,
        )

    refund_transaction = None
    new_status = booking.status
    event_type = BookingEventType.STATUS_CHANGED
    event_comment = ""

    if is_partial:
        remaining_minutes = int(partial_plan["new_time_minutes"])
        new_charge = Decimal(str(partial_plan["new_charge"])).quantize(Decimal("0.01"))
        booking.input_values = partial_plan["new_input_values"]
        booking.charge_breakdown = partial_plan["new_breakdown"]

        booking.total_time_minutes = remaining_minutes
        booking.total_charge = max(Decimal("0.00"), new_charge)
        sync_booking_quota_fields_after_partial_cancel(
            booking,
            planned_minutes=int(booking.total_time_minutes or 0),
            planned_charge=booking.total_charge,
        )
        if cancel_notes:
            note_tag = "[Partial Cancellation Notes]"
            booking.notes = f"{booking.notes or ''}\n{note_tag}: {cancel_notes}".strip()
        booking.save(
            update_fields=[
                "total_time_minutes",
                "total_charge",
                "notes",
                "input_values",
                "charge_breakdown",
            ]
        )

        cancel_item_ids = partial_plan.get("print_analysis_ids_to_cancel")
        if cancel_item_ids:
            PrintAnalysis.objects.filter(
                id__in=cancel_item_ids,
                booking=booking,
            ).update(cancelled_at=timezone.now())

        if should_refund and refund_amount > 0:
            refund_target, _ = WalletRepository.get_booking_wallet_target(
                booking.user, getattr(booking.equipment, "internal_department", None)
            )
            if refund_target:
                eq_name = (
                    getattr(booking.equipment, "name", None)
                    or getattr(booking.equipment, "code", None)
                    or ""
                )
                refund_description = (
                    f"Partial refund for cancelled slot(s) on Booking "
                    f"{booking_display_id_for_email(booking)}- {eq_name}"
                )
                if cancel_notes:
                    refund_description += f" - {cancel_notes}"
                if student_booking_description_suffix_fn:
                    refund_description += student_booking_description_suffix_fn(
                        refund_target, booking.user
                    )
                refund_transaction = refund_target.credit(
                    amount=refund_amount,
                    description=refund_description,
                    related_user=booking.user,
                )

        if cancelled_slots:
            slot_label = ", ".join(
                timezone.localtime(s.start_datetime).strftime("%Y-%m-%d %H:%M")
                for s in cancelled_slots
            )
            event_comment = (
                f"Partial cancellation by {cancelled_by_label}: "
                f"{len(cancelled_slots)} slot(s) released ({slot_label})."
            )
        else:
            event_comment = (
                f"Partial cancellation by {cancelled_by_label}: "
                "booking inputs and charge revised (no slots released)."
            )
        if partial_plan.get("new_input_values"):
            revised = partial_plan["new_input_values"]
            parts = [f"{k}={v}" for k, v in sorted(revised.items()) if str(k).isalpha() and len(str(k)) == 1]
            if parts:
                event_comment += f" Revised inputs: {', '.join(parts)}."
        if should_refund and refund_amount > 0:
            event_comment += f" ₹{refund_amount} refunded to wallet."
        elif cancel_notes:
            event_comment += f" {cancel_notes}"

        create_booking_event(
            booking=booking,
            event_type=event_type,
            previous_status=previous_status,
            new_status=new_status,
            comment=event_comment.strip(),
            created_by=actor,
            send_notification=True,
        )

        return {
            "is_full_cancel": False,
            "released_slot_ids": slot_ids,
            "refund_transaction": refund_transaction,
            "refund_amount": refund_amount,
            "new_status": new_status,
            "previous_status": previous_status,
        }

    # Full cancellation (existing behaviour)
    if cancel_notes:
        tag = "[Cancellation Notes]" if cancelled_by_label != "user" else "[User Cancellation Notes]"
        booking.notes = f"{booking.notes or ''}\n{tag}: {cancel_notes}".strip()

    if should_refund:
        refund_target, _ = WalletRepository.get_booking_wallet_target(
            booking.user, getattr(booking.equipment, "internal_department", None)
        )
        if refund_target:
            refund_amount = Decimal(str(booking.total_charge or "0"))
            eq_name = (
                getattr(booking.equipment, "name", None)
                or getattr(booking.equipment, "code", None)
                or ""
            )
            refund_description = (
                f"Refund for cancelled Booking {booking_display_id_for_email(booking)}- {eq_name}"
            )
            if cancel_notes:
                refund_description += f" - {cancel_notes}"
            if student_booking_description_suffix_fn:
                refund_description += student_booking_description_suffix_fn(
                    refund_target, booking.user
                )
            if refund_amount > 0:
                refund_transaction = refund_target.credit(
                    amount=refund_amount,
                    description=refund_description,
                    related_user=booking.user,
                )
            if previous_status == BookingStatus.DISRUPTION_PENDING:
                if getattr(booking, "disruption_kind", None) == BookingDisruptionKind.OPERATOR_ABSENT:
                    booking.status = BookingStatus.ABSENT
                    new_status = BookingStatus.ABSENT
                    event_type = BookingEventType.ABSENT
                elif getattr(booking, "disruption_kind", None) == BookingDisruptionKind.OTHER_DISRUPTION:
                    booking.status = BookingStatus.OTHER_DISRUPTION
                    new_status = BookingStatus.OTHER_DISRUPTION
                    event_type = BookingEventType.CANCELLED
                else:
                    booking.status = BookingStatus.UNDER_MAINTENANCE
                    new_status = BookingStatus.UNDER_MAINTENANCE
                    event_type = BookingEventType.CANCELLED
            else:
                booking.status = BookingStatus.REFUNDED
                new_status = BookingStatus.REFUNDED
                event_type = BookingEventType.REFUNDED
            label = "admin" if cancelled_by_label == "admin" else "user"
            event_comment = (
                f"Booking cancelled and refunded by {label}. "
                f"{cancel_notes if cancel_notes else ''}"
            )
            if reverse_reward_points_fn:
                reverse_reward_points_fn(
                    booking,
                    actor,
                    "Reward points reversed for cancelled+refunded booking",
                )
        else:
            booking.status = BookingStatus.CANCELLED
            new_status = BookingStatus.CANCELLED
            event_type = BookingEventType.CANCELLED
            event_comment = (
                f"Booking cancelled by {cancelled_by_label} "
                f"(refund requested but wallet not found). "
                f"{cancel_notes if cancel_notes else ''}"
            )
    else:
        booking.status = BookingStatus.CANCELLED
        new_status = BookingStatus.CANCELLED
        event_type = BookingEventType.CANCELLED
        event_comment = (
            f"Booking cancelled by {cancelled_by_label}. "
            f"{cancel_notes if cancel_notes else ''}"
        )

    if getattr(booking, "maintenance_disruption_flag", False) or previous_status == BookingStatus.DISRUPTION_PENDING:
        clear_disruption_policy_fields(booking)

    booking.save()

    create_booking_event(
        booking=booking,
        event_type=event_type,
        previous_status=previous_status,
        new_status=new_status,
        comment=event_comment.strip() if event_comment else None,
        created_by=actor,
        send_notification=True,
    )

    return {
        "is_full_cancel": True,
        "released_slot_ids": slot_ids,
        "refund_transaction": refund_transaction,
        "refund_amount": refund_amount if should_refund else Decimal("0.00"),
        "new_status": new_status,
        "previous_status": previous_status,
    }
