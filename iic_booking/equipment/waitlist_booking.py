"""
Create a booking for a waitlist user when slots become available.
Used by waitlist.process_waitlist_auto_book to auto-assign slots FCFS.
"""

import logging
from datetime import datetime
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from .models import (
    Equipment,
    DailySlot,
    SlotStatus,
    Booking,
    BookingStatus,
    ChargeProfile,
    ChargeProfilePricingProfile,
    BookingChargeSetting,
    initial_istem_fbr_fields_for_charge_profile,
)
from .calculators import (
    ChargeCalculationEngine,
    build_safe_input_values_for_charge_calculation,
    TimeCalculationEngine,
)
from .slot_utils import SlotAvailabilityChecker
from .quota_utils import QuotaChecker, booking_quota_should_skip
from .booking_events import create_booking_event
from .models import BookingEventType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.repositories.wallet_repository import WalletRepository

logger = logging.getLogger(__name__)


def _format_datetime_for_email(dt: datetime | None) -> str:
    """Local date/time string for transactional emails (aligned with booking_events slot formatting)."""
    if not dt:
        return ""
    try:
        dt_local = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    except Exception:
        dt_local = dt
    return dt_local.strftime("%Y-%m-%d %H:%M:%S")


def _get_external_gst_percent():
    """Return GST percentage for external users. Default 18."""
    try:
        obj = BookingChargeSetting.objects.filter(key="EXTERNAL_GST_PERCENT").first()
        if obj and obj.value:
            return Decimal(obj.value.strip())
    except Exception:
        pass
    return Decimal("18")


def _student_booking_description_suffix(wallet_target, booking_user):
    """Suffix for transaction description when student uses faculty wallet."""
    if not wallet_target or not booking_user:
        return ""
    wallet_owner = getattr(getattr(wallet_target, "wallet", None), "user", None)
    if not wallet_owner or wallet_owner.id == booking_user.id:
        return ""
    student_label = (getattr(booking_user, "name", None) or getattr(booking_user, "email", "") or "").strip() or f"User #{booking_user.id}"
    return f" - Student: {student_label}"


def _resolve_charge_profile_for_user(equipment: Equipment, booking_user):
    """
    Resolve the active charge profile for a user (STANDARD/DISCOUNTED logic).
    Returns (charge_profile, user_type, is_external) or (None, user_type, is_external).
    """
    user_type = getattr(booking_user, "user_type", None) or UserType.STUDENT
    is_external = UserType.is_external_user(user_type)

    pricing_profile = ChargeProfilePricingProfile.STANDARD
    if bool(getattr(booking_user, "use_discounted_charge_profile", False)):
        from .models import UserDiscountedChargeEquipment

        overrides_exist = UserDiscountedChargeEquipment.objects.filter(
            user=booking_user, is_active=True
        ).exists()
        if not overrides_exist:
            pricing_profile = ChargeProfilePricingProfile.DISCOUNTED
        else:
            overridden = UserDiscountedChargeEquipment.objects.filter(
                user=booking_user, equipment=equipment, is_active=True
            ).exists()
            pricing_profile = (
                ChargeProfilePricingProfile.DISCOUNTED
                if overridden
                else ChargeProfilePricingProfile.STANDARD
            )

    try:
        charge_profile = ChargeProfile.objects.get(
            equipment=equipment,
            user_type=user_type,
            pricing_profile=pricing_profile,
            is_active=True,
        )
        return charge_profile, user_type, is_external
    except ChargeProfile.DoesNotExist:
        return None, user_type, is_external


def reduce_waitlist_inputs_to_fit_available_slots(
    equipment: Equipment,
    booking_user,
    *,
    input_values: dict | None,
    selected_parameters=None,
    desired_slots: int,
    max_slots_available: int,
):
    """
    Try to reduce the waitlisted request to fit within max_slots_available slots (>= 1).
    Uses the same reduction concept as "Book even if single slot is available":
    - HOUR profile: reduce key 'B' (number of slots)
    - Others (SAMPLE/SAMPLE_ELEMENT/MULTI_PARAM): reduce key 'A' (samples)

    Returns:
        (reduced_input_values, effective_time_minutes, slots_to_book) or None
    """
    slot_duration = int(getattr(equipment, "slot_duration_minutes", None) or 60)
    if slot_duration <= 0:
        slot_duration = 60

    desired_slots = int(desired_slots or 1)
    max_slots_available = int(max_slots_available or 0)
    if max_slots_available <= 0:
        return None

    charge_profile, user_type, _is_external = _resolve_charge_profile_for_user(equipment, booking_user)
    if not charge_profile:
        return None

    class ChargeProfileWithType:
        def __init__(self, cp, equip):
            self.equipment = cp.equipment
            self.user_type = cp.user_type
            self.is_active = cp.is_active
            self.primary_unit_charge = cp.primary_unit_charge
            self.secondary_unit_charge = cp.secondary_unit_charge
            self.breakpoint = cp.breakpoint
            self.time_formula = cp.time_formula
            self.pricing_profile = getattr(cp, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
            self.profile_type = getattr(equip, "profile_type", None)

    cp_with_type = ChargeProfileWithType(charge_profile, equipment)

    def reduce_key_for_profile(profile_type: str | None) -> str | None:
        # Match api_views._get_reduce_key_for_single_slot
        pt = (profile_type or "").strip().upper()
        if pt == "HOUR":
            return "B"
        if pt in ("SAMPLE", "SAMPLE_ELEMENT", "MULTI_PARAM"):
            return "A"
        return None

    base_inputs = dict(input_values or {})
    key = reduce_key_for_profile(getattr(equipment, "profile_type", None))
    if not key:
        return None

    max_minutes_available = max_slots_available * slot_duration
    if max_minutes_available <= 0:
        return None

    def ceil_div(a: int, b: int) -> int:
        return (a + b - 1) // b if b > 0 else 0

    # HOUR profile: key 'B' represents number of slots — shrink B until it fits.
    if key == "B":
        upper = min(desired_slots, max_slots_available)
        for n in range(upper, 0, -1):
            candidate = dict(base_inputs)
            candidate["B"] = n
            safe_inputs = build_safe_input_values_for_charge_calculation(candidate, equipment=equipment)
            try:
                effective_time = int(
                    TimeCalculationEngine.calculate_time(
                        cp_with_type,
                        safe_inputs,
                        slot_duration_minutes=slot_duration,
                    )
                )
            except Exception:
                continue
            if effective_time <= 0:
                continue
            # Must fit within available slots window.
            if effective_time > max_minutes_available:
                continue
            slots_needed = ceil_div(effective_time, slot_duration)
            # Never accept a 0-slot "fit" even if the time engine returns a quirky value.
            if slots_needed < 1 or slots_needed > max_slots_available:
                continue
            return safe_inputs, effective_time, slots_needed
        return None

    # SAMPLE / SAMPLE_ELEMENT / MULTI_PARAM: reduce 'A' inside a loop until time fits available slots.
    # Only time calculation is performed inside the loop; charge and other validations happen later.
    start_a = base_inputs.get("A")
    try:
        start_a_int = int(start_a) if start_a is not None else int(desired_slots or 1)
    except Exception:
        start_a_int = int(desired_slots or 1)
    start_a_int = max(1, start_a_int)

    for a in range(start_a_int, 0, -1):
        candidate = dict(base_inputs)
        candidate["A"] = a
        safe_inputs = build_safe_input_values_for_charge_calculation(candidate, equipment=equipment)
        try:
            effective_time = int(
                TimeCalculationEngine.calculate_time(
                    cp_with_type,
                    safe_inputs,
                    slot_duration_minutes=slot_duration,
                )
            )
        except Exception:
            continue
        if effective_time <= 0:
            continue
        if effective_time > max_minutes_available:
            continue
        slots_needed = ceil_div(effective_time, slot_duration)
        if slots_needed < 1 or slots_needed > max_slots_available:
            continue
        return safe_inputs, effective_time, slots_needed

    return None


def create_booking_for_waitlist_user(
    equipment: Equipment,
    booking_user,
    slot_ids: list,
    created_by=None,
    *,
    waitlist_queue_position: int | None = None,
    input_values: dict | None = None,
    selected_parameters=None,
    total_time_minutes_override: int | None = None,
    requirement_note: str | None = None,
    waitlist_joined_at: datetime | None = None,
):
    """
    Create one booking for the given user with the given slot IDs (e.g. one slot for waitlist).
    Debits wallet, creates booking, assigns slots, sends waitlist confirmation (user + wallet owner).

    Returns:
        (booking, None) on success
        (None, error_message) on failure
    """
    if not slot_ids:
        return None, "No slot IDs provided"
    if created_by is None:
        created_by = booking_user

    charge_profile, user_type, is_external = _resolve_charge_profile_for_user(equipment, booking_user)
    if not charge_profile:
        return None, f"No active charge profile for equipment and user type {user_type}."

    istem_required_msg = (
        "I-STEM portal registration must be confirmed on your profile before a waitlist booking can be confirmed. "
        "Open Profile, confirm I-STEM registration, save, then try again."
    )
    # Cheap early exit; authoritative re-check is inside the atomic block below
    # (acknowledgement is user-editable on Profile and can change between requests).
    if is_external and not getattr(booking_user, "istem_portal_acknowledged", False):
        return None, istem_required_msg

    base_filter = {
        "id__in": slot_ids,
        "slot_master__equipment": equipment,
        "status": SlotStatus.AVAILABLE,
    }
    if is_external:
        base_filter["reserved_for_external"] = True
    else:
        base_filter["reserved_for_external"] = False

    checker = (
        SlotAvailabilityChecker.is_slot_available_for_external
        if is_external
        else SlotAvailabilityChecker.is_slot_available
    )

    # Pre-lock availability check (cheap early-exit). The select_for_update inside
    # transaction.atomic() is the authoritative anti-double-booking guard.
    daily_slots = DailySlot.objects.filter(**base_filter).order_by("start_datetime")
    from .slot_department_access import filter_queryset_for_home_department

    daily_slots = filter_queryset_for_home_department(
        daily_slots,
        user=booking_user,
        equipment=equipment,
        is_admin=False,
        is_external=is_external,
    )
    if daily_slots.count() != len(slot_ids):
        return None, "One or more slots are invalid or not available."
    unavailable = [s.id for s in daily_slots if not checker(s)]
    if unavailable:
        return None, f"Slots {unavailable} are not available for booking."
    # Also reject restricted home-department-only slots explicitly
    from .slot_department_access import slot_allows_internal_user

    if not is_external:
        denied = [s.id for s in daily_slots if not slot_allows_internal_user(s, booking_user, equipment)]
        if denied:
            return None, (
                f"Slots {denied} are not available for your department under home / non-home reservation rules."
            )

    total_slot_minutes = sum(
        int((s.end_datetime - s.start_datetime).total_seconds() / 60)
        for s in daily_slots
        if s.start_datetime and s.end_datetime
    )
    if total_slot_minutes <= 0:
        return None, "Invalid slot duration."

    total_time_minutes = int(total_time_minutes_override) if total_time_minutes_override is not None else int(total_slot_minutes)
    if total_time_minutes <= 0 or total_time_minutes > total_slot_minutes:
        return None, "Requested duration does not fit within available slots."
    start_time = daily_slots.first().start_datetime
    booking_date = start_time

    class ChargeProfileWithType:
        def __init__(self, cp, equip):
            self.equipment = cp.equipment
            self.user_type = cp.user_type
            self.is_active = cp.is_active
            self.primary_unit_charge = cp.primary_unit_charge
            self.secondary_unit_charge = cp.secondary_unit_charge
            self.breakpoint = cp.breakpoint
            self.time_formula = cp.time_formula
            self.pricing_profile = getattr(cp, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
            self.profile_type = getattr(equip, "profile_type", None)

    charge_profile_with_type = ChargeProfileWithType(charge_profile, equipment)
    safe_input_values = build_safe_input_values_for_charge_calculation(input_values or {}, equipment=equipment)
    try:
        total_charge, charge_breakdown = ChargeCalculationEngine.calculate_charge(
            charge_profile_with_type,
            safe_input_values,
            total_time_minutes,
            selected_parameters=selected_parameters,
        )
    except Exception as e:
        return None, f"Error calculating charge: {e}"

    if is_external:
        gst_percent = _get_external_gst_percent()
        if gst_percent > 0:
            gst_amount = (total_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
            total_charge = (total_charge + gst_amount).quantize(Decimal("0.01"))
            charge_breakdown = list(charge_breakdown) + [
                {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
            ]

    if not booking_quota_should_skip(equipment):
        for quota_type in ("WEEKLY", "MONTHLY"):
            quota_allowed, quota_error = QuotaChecker.check_user_quota(
                user=booking_user,
                equipment=equipment,
                quota_type=quota_type,
                additional_time_minutes=total_time_minutes,
                additional_bookings=1,
                additional_charge=total_charge,
                booking_date=booking_date,
            )
            if not quota_allowed:
                return None, f"{quota_type} quota: {quota_error}"

    booking_target, _ = WalletRepository.get_booking_wallet_target(
        booking_user, getattr(equipment, "internal_department", None)
    )
    if not booking_target:
        return None, "User does not have access to any wallet."
    booking_target.refresh_from_db()
    from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

    ok_w, w_err = subwallet_booking_balance_ok(booking_target, total_charge, False)
    if not ok_w:
        return None, w_err or "Insufficient wallet balance"

    notes = "Auto-booked from waitlist (slots became available)."
    extra = (requirement_note or "").strip()
    if extra:
        notes = f"{notes}\n{extra}".strip()
    event_metadata = {"from_waitlist": True}
    if waitlist_queue_position is not None:
        event_metadata["waitlist_position"] = f"WL{int(waitlist_queue_position)}"
    if waitlist_joined_at is not None:
        event_metadata["waitlist_joined_at_display"] = _format_datetime_for_email(waitlist_joined_at)

    try:
        with transaction.atomic():
            locked = list(
                DailySlot.objects.select_for_update(skip_locked=True)
                .filter(**base_filter)
                .order_by("start_datetime")
            )
            if len(locked) != len(slot_ids):
                return None, "One or more slots are no longer available or are being booked by another request."
            bad = [s.id for s in locked if not checker(s)]
            if bad:
                return None, f"Slots {bad} are not available for booking."

            # Re-validate I-STEM under the same lock window as slot claim + debit.
            if is_external:
                booking_user.refresh_from_db(fields=["istem_portal_acknowledged"])
                if not getattr(booking_user, "istem_portal_acknowledged", False):
                    return None, istem_required_msg

            booking_target.refresh_from_db()
            ok_w2, w_err2 = subwallet_booking_balance_ok(booking_target, total_charge, False)
            if not ok_w2:
                return None, w_err2 or "Insufficient wallet balance"

            if total_charge > 0:
                from iic_booking.users.wallet_credit_facility import subwallet_minimum_balance_after_debit

                transaction_description = (
                    f"Booking #{equipment.code} - {equipment.name} ({total_time_minutes} minutes)"
                    + _student_booking_description_suffix(booking_target, booking_user)
                )
                booking_target.debit(
                    amount=total_charge,
                    description=transaction_description,
                    related_user=booking_user,
                    minimum_balance_after=subwallet_minimum_balance_after_debit(booking_target),
                )
            booking = Booking.objects.create(
                user=booking_user,
                equipment=equipment,
                charge_profile=charge_profile,
                user_type_snapshot=user_type,
                total_time_minutes=total_time_minutes,
                total_charge=total_charge,
                input_values=safe_input_values,
                selected_parameters=selected_parameters,
                charge_breakdown=charge_breakdown,
                status=BookingStatus.BOOKED,
                notes=notes,
                created_by=created_by,
                **initial_istem_fbr_fields_for_charge_profile(charge_profile),
            )
            slot_pks = [s.pk for s in locked]
            DailySlot.objects.filter(pk__in=slot_pks).update(booking=booking, status=SlotStatus.BOOKED)
            waitlist_code = event_metadata.get("waitlist_position")
            if waitlist_code:
                create_booking_event(
                    booking=booking,
                    event_type=BookingEventType.COMMENT,
                    created_by=created_by,
                    comment=f"Booking waitlisted at queue position {waitlist_code}.",
                    send_notification=False,
                    metadata={
                        "from_waitlist": True,
                        "waitlist_position": waitlist_code,
                    },
                )
                create_booking_event(
                    booking=booking,
                    event_type=BookingEventType.STATUS_CHANGED,
                    created_by=created_by,
                    previous_status=BookingStatus.WAITLISTED,
                    new_status=BookingStatus.BOOKED,
                    comment=f"Status changed from Waitlisted ({waitlist_code}) to Confirmed.",
                    send_notification=False,
                    metadata={
                        "from_waitlist": True,
                        "waitlist_position": waitlist_code,
                    },
                )
            create_booking_event(
                booking=booking,
                event_type=BookingEventType.CREATED,
                created_by=created_by,
                comment=f"Booking created from waitlist for {equipment.name} ({total_time_minutes} minutes, ₹{total_charge:.2f}).",
                new_status=booking.status,
                send_notification=True,
                metadata=event_metadata,
            )
        return booking, None
    except Exception as e:
        logger.exception("Waitlist auto-booking failed for user %s equipment %s: %s", booking_user.id, equipment.pk, e)
        return None, str(e)
