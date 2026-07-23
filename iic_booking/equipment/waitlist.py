"""
Waitlist queue service for equipment.

- Add user to waitlist on failed booking (respects equipment.waitlist_queue_depth).
- Send "unsuccessful booking + your position in queue" email.
- When slots become available (cancellation, refund, operator unavailable, admin/OIC marking slots AVAILABLE):
  automatically create bookings for waitlisted users one by one (FCFS): wallet debit, BOOKED row,
  slots assigned, and email to the booker and wallet owner (template booking_waitlist_confirmed_email).
- Before the pre-reference cutoff window, FCFS is attempted once so users with available slots are
  confirmed before remaining waitlist rows are cleared.
"""

import logging
from datetime import datetime, timedelta
from typing import Tuple, Optional

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.models import (
    Equipment,
    WaitlistEntry,
    DailySlot,
    SlotStatus,
    BookingAttemptLog,
    BookingAttemptOutcome,
    DynamicInputField,
)
from iic_booking.equipment.waitlist_booking import (
    create_booking_for_waitlist_user,
    reduce_waitlist_inputs_to_fit_available_slots,
)
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import get_frontend_absolute_url
from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)


def should_trigger_waitlist_after_slot_release(
    equipment: Equipment, preferred_slot_ids: list[int] | None = None
) -> bool:
    """
    Decide whether FCFS waitlist auto-booking should run for a slot-release event.

    If a released slot starts too soon (inside equipment.reschedule_hours_threshold),
    skip FCFS so users still have enough preparation time.
    """
    slot_ids = [int(x) for x in (preferred_slot_ids or []) if x is not None]
    if not slot_ids:
        return True

    threshold_hours = int(getattr(equipment, "reschedule_hours_threshold", 0) or 0)
    if threshold_hours <= 0:
        return True

    now = timezone.now()
    threshold_delta = timedelta(hours=threshold_hours)
    released_slots = DailySlot.objects.filter(
        id__in=slot_ids,
        slot_master__equipment=equipment,
    ).only("id", "start_datetime")

    for slot in released_slots:
        start_dt = getattr(slot, "start_datetime", None)
        if not start_dt:
            continue
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        lead_time = start_dt - now
        if lead_time < threshold_delta:
            return False
    return True


def schedule_waitlist_slots_available_after_commit(
    equipment: Equipment,
    preferred_slot_ids: list[int] | None = None,
    *,
    respect_reschedule_threshold: bool = False,
) -> None:
    """
    Run waitlist FCFS after the current DB transaction commits.

    Required when ATOMIC_REQUESTS wraps the request: slot releases from partial or
    full cancellation must be visible before auto-booking waitlisted users.
    """
    slot_ids = [int(x) for x in (preferred_slot_ids or []) if x is not None]
    if not slot_ids:
        return
    equipment_id = int(equipment.pk)

    def _run():
        try:
            eq = Equipment.objects.get(pk=equipment_id)
            notify_waitlist_slots_available(
                eq,
                preferred_slot_ids=slot_ids,
                respect_reschedule_threshold=respect_reschedule_threshold,
            )
        except Exception:
            logger.exception(
                "Failed waitlist FCFS after slot release commit for equipment %s",
                equipment_id,
            )

    transaction.on_commit(_run)


def _format_waitlist_code(position: int) -> str:
    """Convert numeric position to WL code, e.g. 1 -> WL1."""
    return f"WL{int(position)}"


def waitlist_virtual_booking_id(
    equipment_code: str,
    position: int,
    *,
    department_code: str = "",
    created_at=None,
) -> str:
    """
    Display ID aligned with normal booking virtual_booking_id (DEPT + CODE + YEAR + 5-digit number)
    plus trailing W to mark waitlisted (not a confirmed booking).

    Example: CHSEM202600042W (same shape as CHSEM202600042 for a confirmed booking, with W suffix).
    """
    if created_at is not None and getattr(created_at, "year", None):
        year = int(created_at.year)
    else:
        year = int(timezone.now().year)
    dept = (department_code or "").strip()
    code = (equipment_code or "").strip() or "UNK"
    pos = max(1, int(position or 1))
    pos = min(pos, 99999)
    return f"{dept}{code}{year}{pos:05d}W"


def _get_latest_waitlist_attempt_payload(
    user, equipment: Equipment
) -> tuple[dict, list | None, int | None, int | None]:
    """
    Reconstruct calculation inputs from the latest failed booking attempt.
    additional_info may store input_values by field LABEL; convert labels back to field KEYs.
    """
    log = (
        BookingAttemptLog.objects.filter(
            user=user,
            equipment=equipment,
            outcome=BookingAttemptOutcome.FAILED,
        )
        .order_by("-requested_at")
        .first()
    )
    if not log or not isinstance(log.additional_info, dict):
        return {}, None, None, None

    info = log.additional_info or {}
    raw_inputs = info.get("input_values")
    selected_parameters = info.get("selected_parameters")
    if not isinstance(raw_inputs, dict):
        return {}, selected_parameters if isinstance(selected_parameters, list) else None, None, None

    label_to_key = {}
    try:
        rows = DynamicInputField.objects.filter(equipment=equipment).values_list("field_key", "field_label")
        for key, label in rows:
            if key:
                label_to_key[str(key)] = str(key)
            if label:
                label_to_key[str(label)] = str(key)
    except Exception:
        pass

    normalized = {}
    for k, v in raw_inputs.items():
        key = label_to_key.get(str(k), str(k))
        normalized[key] = v

    slots_requested = None
    duration_minutes = None
    try:
        slots_requested = int(getattr(log, "slots_requested", None) or 0) or None
    except Exception:
        slots_requested = None
    try:
        duration_minutes = int(getattr(log, "duration_minutes", None) or 0) or None
    except Exception:
        duration_minutes = None

    if isinstance(selected_parameters, list):
        return normalized, selected_parameters, slots_requested, duration_minutes
    if isinstance(selected_parameters, str) and selected_parameters.strip():
        return normalized, [selected_parameters.strip()], slots_requested, duration_minutes
    return normalized, None, slots_requested, duration_minutes


def add_user_to_waitlist(equipment: Equipment, user) -> Tuple[bool, Optional[int]]:
    """
    Add user to the equipment waitlist if enabled and queue not full.
    Uses (equipment, user) unique - if user already in list, return (False, position).

    Returns:
        (added: bool, position: int | None)
        - (True, N): user was added at position N (1-based).
        - (False, N): user already in list at position N.
        - (False, None): waitlist disabled or queue full, not added.
    """
    depth = getattr(equipment, "waitlist_queue_depth", None) or 0
    if depth <= 0:
        return False, None

    with transaction.atomic():
        existing = WaitlistEntry.objects.filter(equipment=equipment, user=user).first()
        if existing:
            # If the user was previously marked CANNOT_FULFILL, allow rejoining by re-activating
            # and moving them to the end of the ACTIVE queue (so WL position is never WL0).
            if (getattr(existing, "status", None) or "ACTIVE").strip().upper() != "ACTIVE":
                existing.status = "ACTIVE"
                existing.cannot_fulfill_remark = None
                existing.marked_cannot_fulfill_at = None
                # Move to end of queue for fairness on rejoin.
                existing.created_at = timezone.now()
                existing.save(
                    update_fields=[
                        "status",
                        "cannot_fulfill_remark",
                        "marked_cannot_fulfill_at",
                        "created_at",
                    ]
                )
            position = (
                WaitlistEntry.objects.filter(
                    equipment=equipment, status="ACTIVE", created_at__lte=existing.created_at
                ).count()
            )
            return False, max(1, int(position or 0))

        current_count = WaitlistEntry.objects.filter(equipment=equipment, status="ACTIVE").count()
        if current_count >= depth:
            return False, None

        entry = WaitlistEntry.objects.create(equipment=equipment, user=user, status="ACTIVE")
        position = (
            WaitlistEntry.objects.filter(
                equipment=equipment, status="ACTIVE", created_at__lte=entry.created_at
            ).count()
        )
        return True, max(1, int(position or 0))


def send_unsuccessful_booking_waitlist_email(user, equipment: Equipment, position: int, failure_reason: str = ""):
    """Send email to user: booking unsuccessful, you have been added to the waitlist at position X."""
    try:
        requested_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
        CommunicationService.send_email(
            recipient=user,
            template="booking_unsuccessful_waitlist_email",
            template_context={
                "user_name": getattr(user, "name", None) or getattr(user, "email", "User"),
                "user_email": getattr(user, "email", ""),
                "equipment_name": getattr(equipment, "name", None) or getattr(equipment, "code", "Equipment"),
                "equipment_code": getattr(equipment, "code", ""),
                "waitlist_position": _format_waitlist_code(position),
                "waitlist_requested_at": requested_at,
                "failure_reason": failure_reason or "The selected slots were not available.",
            },
        )
    except Exception as e:
        logger.exception("Failed to send waitlist position email to %s: %s", getattr(user, "email", ""), e)


def send_waitlist_unsuccessful_email(user, equipment: Equipment, position: int, failure_reason: str = ""):
    """
    Send email when waitlisted booking cannot be confirmed during queue clearing
    (e.g. insufficient balance).
    """
    try:
        requested_at = timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
        CommunicationService.send_email(
            recipient=user,
            template="booking_unsuccessful_waitlist_email",
            template_context={
                "user_name": getattr(user, "name", None) or getattr(user, "email", "User"),
                "user_email": getattr(user, "email", ""),
                "equipment_name": getattr(equipment, "name", None) or getattr(equipment, "code", "Equipment"),
                "equipment_code": getattr(equipment, "code", ""),
                "waitlist_position": _format_waitlist_code(position),
                "waitlist_requested_at": requested_at,
                "failure_reason": failure_reason or "Your waitlisted booking could not be confirmed.",
            },
        )
    except Exception as e:
        logger.exception("Failed to send waitlist unsuccessful email to %s: %s", getattr(user, "email", ""), e)


def _format_local_dt(dt: datetime | None, fmt: str) -> str:
    if not dt:
        return ""
    try:
        dt_local = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    except Exception:
        dt_local = dt
    return dt_local.strftime(fmt)


def _resolve_equipment_contacts_for_short_notice_email(equipment: Equipment) -> str:
    """
    Build a single contact line for short-notice waitlist emails.
    Prefers lab operator, then OIC/manager(s). Falls back to a generic prompt.
    """
    try:
        from .models import EquipmentOperator, EquipmentManager

        eq_id = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
        parts: list[str] = []

        op_link = (
            EquipmentOperator.objects.filter(equipment_id=eq_id)
            .select_related("operator")
            .order_by("equipment_operator_id")
            .first()
        )
        op_user = getattr(op_link, "operator", None) if op_link else None
        if op_user:
            name = (getattr(op_user, "name", "") or getattr(op_user, "email", "") or "Lab operator").strip()
            phone = (getattr(op_user, "phone_number", "") or "").strip()
            parts.append(f"Lab operator: {name}" + (f" ({phone})" if phone else ""))

        mgr_links = (
            EquipmentManager.objects.filter(equipment_id=eq_id)
            .select_related("manager")
            .order_by("equipment_manager_id")
        )
        for link in mgr_links[:2]:
            m = getattr(link, "manager", None)
            if not m:
                continue
            name = (getattr(m, "name", "") or getattr(m, "email", "") or "OIC").strip()
            phone = (getattr(m, "phone_number", "") or "").strip()
            parts.append(f"OIC: {name}" + (f" ({phone})" if phone else ""))

        out = "; ".join([p for p in parts if p.strip()])
        return out or "Please contact the lab operator / OIC."
    except Exception:
        return "Please contact the lab operator / OIC."


def _notify_waitlist_short_notice_slot_available(
    equipment: Equipment, *, entries: list[WaitlistEntry], slot_start_dt: datetime, lead_hours: float
) -> int:
    """
    Short-notice slot availability: do not auto-book; email all ACTIVE waitlist users.
    """
    equipment_name = getattr(equipment, "name", None) or getattr(equipment, "code", "Equipment")
    link = get_frontend_absolute_url("/equipments") or "/equipments"
    contact_line = _resolve_equipment_contacts_for_short_notice_email(equipment)

    date_str = _format_local_dt(slot_start_dt, "%Y-%m-%d")
    time_str = _format_local_dt(slot_start_dt, "%H:%M")
    # Integer hours only (e.g. 1, 2, 3). Use ceiling so "0.2 hours" becomes "1 hour".
    try:
        import math

        lead_hours_int = int(math.ceil(max(0.0, float(lead_hours))))
    except Exception:
        lead_hours_int = 0
    lead_hours_str = str(lead_hours_int)

    sent = 0
    for entry in entries:
        user = getattr(entry, "user", None)
        if not user:
            continue
        try:
            CommunicationService.send_email(
                recipient=user,
                template="waitlist_short_notice_slot_available_email",
                template_context={
                    "user_name": getattr(user, "name", None) or getattr(user, "email", "User"),
                    "user_email": getattr(user, "email", ""),
                    "equipment_name": equipment_name,
                    "lead_hours": lead_hours_str,
                    "slot_date": date_str,
                    "slot_time": time_str,
                    "contact_line": contact_line,
                    "link": link,
                },
            )
            sent += 1
        except Exception:
            logger.exception(
                "Failed to send short-notice slot-available email to %s (equipment %s)",
                getattr(user, "email", ""),
                getattr(equipment, "code", equipment.pk),
            )
    return sent


def notify_waitlist_slots_available(
    equipment: Equipment,
    preferred_slot_ids: list[int] | None = None,
    respect_reschedule_threshold: bool = False,
) -> int:
    """
    When slots become available, automatically create bookings for waitlisted users
    one by one (first-come-first-serve). Booking confirmation is sent to each user
    and to the faculty wallet owner (via create_booking_event). Then clear the waitlist.
    Called when slots become available due to cancellation, rescheduling, or admin/OIC
    marking slots AVAILABLE. Returns the number of bookings created.

    Note: only waitlist entries we attempted to auto-book are removed. Entries that
    couldn't be booked because no slots were available for their user type remain.
    """
    preferred_slot_ids = [int(x) for x in (preferred_slot_ids or []) if x is not None]
    entries = list(
        WaitlistEntry.objects.filter(equipment=equipment, status="ACTIVE")
        .select_related("user", "equipment")
        .order_by("created_at")
    )
    if not entries:
        return 0

    if respect_reschedule_threshold and not should_trigger_waitlist_after_slot_release(
        equipment, preferred_slot_ids
    ):
        # Short notice: do NOT auto-book / clear. Notify all waitlist users that a slot is available.
        now = timezone.now()
        start_dt = None
        try:
            start_dt = (
                DailySlot.objects.filter(id__in=preferred_slot_ids, slot_master__equipment=equipment)
                .exclude(start_datetime__isnull=True)
                .order_by("start_datetime")
                .values_list("start_datetime", flat=True)
                .first()
            )
        except Exception:
            start_dt = None
        if not start_dt:
            logger.warning(
                "Waitlist FCFS skipped for equipment %s (short notice) but released slot start time is unavailable.",
                getattr(equipment, "code", equipment.pk),
            )
            return 0
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        lead_hours = (start_dt - now).total_seconds() / 3600.0
        logger.warning(
            "Waitlist FCFS skipped for equipment %s: released slot starts in %.2f hour(s) "
            "(inside reschedule_hours_threshold). Emailing waitlist users instead.",
            getattr(equipment, "code", equipment.pk),
            lead_hours,
        )
        return _notify_waitlist_short_notice_slot_available(
            equipment, entries=entries, slot_start_dt=start_dt, lead_hours=lead_hours
        )

    equipment_code = getattr(equipment, "code", "")
    time_from = getattr(equipment, "weekly_view_time_from", None)
    time_to = getattr(equipment, "weekly_view_time_to", None)
    now = timezone.now()

    # Input field key -> label map (for readable notes).
    key_to_label: dict[str, str] = {}
    try:
        rows = DynamicInputField.objects.filter(equipment=equipment).values_list("field_key", "field_label")
        for k, label in rows:
            ks = (str(k).strip() if k is not None else "")
            if not ks:
                continue
            ls = (str(label).strip() if label is not None else "")
            key_to_label[ks] = ls or ks
    except Exception:
        key_to_label = {}

    def slot_ids_within_window(queryset):
        """Filter slot IDs to those fully within equipment slot window (from/to inclusive)."""
        slots = list(queryset.order_by("start_datetime").only("id", "start_datetime", "end_datetime"))
        if not slots or (time_from is None and time_to is None):
            return [
                s.id for s in slots
                if s.start_datetime and s.start_datetime >= now
            ]
        from django.utils import timezone as tz
        out = []
        for s in slots:
            if not s.start_datetime or not s.end_datetime:
                continue
            if s.start_datetime < now:
                continue
            start_local = tz.localtime(s.start_datetime) if tz.is_aware(s.start_datetime) else s.start_datetime
            end_local = tz.localtime(s.end_datetime) if tz.is_aware(s.end_datetime) else s.end_datetime
            st, et = start_local.time(), end_local.time()
            if time_from is not None and st < time_from:
                continue
            if time_to is not None and et > time_to:
                continue
            out.append(s.id)
        return out

    def prioritize_slots(slot_ids: list[int]) -> list[int]:
        if not preferred_slot_ids or not slot_ids:
            return slot_ids
        slot_set = set(slot_ids)
        # For a slot-release trigger, confirm only against the released slot(s),
        # never against earlier available slots.
        return [sid for sid in preferred_slot_ids if sid in slot_set]

    # Shared AVAILABLE pool for internal and external (quota enforced at booking time).
    available_qs = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        status=SlotStatus.AVAILABLE,
    )
    available_slots = prioritize_slots(slot_ids_within_window(available_qs))
    if not available_slots and available_qs.exists():
        logger.info(
            "Waitlist auto-book: AVAILABLE slots exist but none are within configured weekly window for equipment %s.",
            equipment_code,
        )
    used_slots = set()
    bookings_created = 0
    # Only remove waitlist entries after we successfully auto-book them.
    # If auto-booking fails for a user (e.g. quota/wallet/other business rule),
    # keep the entry so they can be retried in a future auto-booking run.
    processed_entry_ids: list[int] = []
    cannot_fulfill_remark_by_id: dict[int, str] = {}

    for idx, entry in enumerate(entries, start=1):
        user = entry.user
        available_ids = [x for x in available_slots if x not in used_slots]
        if not available_ids:
            logger.debug("No more available slots for waitlist user %s (equipment %s).", user.email, equipment_code)
            continue
        (
            attempt_input_values,
            attempt_selected_parameters,
            attempt_slots_requested,
            attempt_duration_minutes,
        ) = _get_latest_waitlist_attempt_payload(user, equipment)

        slot_duration = int(getattr(equipment, "slot_duration_minutes", None) or 60)
        if slot_duration <= 0:
            slot_duration = 60

        desired_slots = int(attempt_slots_requested or 0) if attempt_slots_requested else 0
        if desired_slots <= 0 and attempt_duration_minutes:
            desired_slots = max(1, (int(attempt_duration_minutes) + slot_duration - 1) // slot_duration)
        if desired_slots <= 0:
            desired_slots = 1

        reduced = reduce_waitlist_inputs_to_fit_available_slots(
            equipment,
            user,
            input_values=attempt_input_values,
            selected_parameters=attempt_selected_parameters,
            desired_slots=desired_slots,
            max_slots_available=len(available_ids),
        )
        if not reduced:
            logger.info(
                "Waitlist auto-book: cannot fit waitlist requirement into %d available slot(s) for user %s (equipment %s). Skipping entry.",
                len(available_ids),
                getattr(user, "email", user.pk),
                equipment_code,
            )
            cannot_fulfill_remark_by_id[int(entry.id)] = (
                f"Cannot fit required duration into currently available slots. "
                f"available_slots={len(available_ids)}"
            )
            continue

        reduced_input_values, effective_time_minutes, slots_to_book = reduced
        slot_ids_to_book = available_ids[: int(slots_to_book)]

        # Add original vs fulfilled requirement in booking notes (when reduced).
        def _fmt_kv(k, v):
            if v is None:
                return None
            s = str(v).strip()
            if not s:
                return None
            label = key_to_label.get(str(k), str(k))
            return f"{label}={s}"

        original_parts = []
        fulfilled_parts = []
        try:
            original_a = (attempt_input_values or {}).get("A")
            original_b = (attempt_input_values or {}).get("B")
            fulfilled_a = (reduced_input_values or {}).get("A")
            fulfilled_b = (reduced_input_values or {}).get("B")
            original_parts.extend([_fmt_kv("A", original_a), _fmt_kv("B", original_b)])
            fulfilled_parts.extend([_fmt_kv("A", fulfilled_a), _fmt_kv("B", fulfilled_b)])
        except Exception:
            pass
        if attempt_slots_requested:
            original_parts.append(f"slots={int(attempt_slots_requested)}")
        if attempt_duration_minutes:
            original_parts.append(f"duration={int(attempt_duration_minutes)}m")
        fulfilled_parts.append(f"slots={int(slots_to_book)}")
        fulfilled_parts.append(f"duration={int(effective_time_minutes)}m")

        original_parts = [p for p in original_parts if p]
        fulfilled_parts = [p for p in fulfilled_parts if p]
        requirement_note = None
        if original_parts and fulfilled_parts and original_parts != fulfilled_parts:
            requirement_note = (
                "Waitlist requirement adjusted. "
                f"Original: {', '.join(original_parts)}. "
                f"Fulfilled: {', '.join(fulfilled_parts)}."
            )

        booking, err = create_booking_for_waitlist_user(
            equipment,
            user,
            slot_ids_to_book,
            created_by=user,
            waitlist_queue_position=idx,
            input_values=reduced_input_values,
            selected_parameters=attempt_selected_parameters,
            total_time_minutes_override=effective_time_minutes,
            requirement_note=requirement_note,
            waitlist_joined_at=getattr(entry, "created_at", None),
        )
        if booking:
            for sid in slot_ids_to_book:
                used_slots.add(sid)
            bookings_created += 1
            processed_entry_ids.append(entry.id)
            logger.info(
                "Waitlist auto-booking: created booking %s for user %s (equipment %s).",
                booking.booking_id,
                user.email,
                equipment_code,
            )
        else:
            logger.warning(
                "Waitlist auto-booking skipped for user %s (equipment %s): %s",
                user.email,
                equipment_code,
                err or "unknown",
            )
            # Mark cannot-fulfill when booking fails due to business constraints.
            a_val = None
            try:
                a_val = (reduced_input_values or {}).get("A")
            except Exception:
                a_val = None
            cannot_fulfill_remark_by_id[int(entry.id)] = (
                f"Auto-book failed after reduction"
                + (f" (A={a_val})" if a_val is not None else "")
                + f": {err or 'unknown'}"
            )
            # No notification is sent here because waitlist allocation is handled automatically.
            # Keeping the entry ensures they can be retried when a future slot-release event occurs.

    if cannot_fulfill_remark_by_id:
        try:
            now_mark = timezone.now()
            for entry_id, remark in cannot_fulfill_remark_by_id.items():
                WaitlistEntry.objects.filter(equipment=equipment, id=entry_id).update(
                    status="CANNOT_FULFILL",
                    cannot_fulfill_remark=(remark or "")[:2000],
                    marked_cannot_fulfill_at=now_mark,
                )
        except Exception:
            logger.exception("Failed to mark cannot-fulfill waitlist entries for equipment %s", equipment_code)

    if processed_entry_ids:
        with transaction.atomic():
            deleted, _ = WaitlistEntry.objects.filter(equipment=equipment, id__in=processed_entry_ids).delete()
            if deleted:
                logger.info(
                    "Cleared processed waitlist entries for equipment %s after creating %d booking(s) (FCFS).",
                    equipment_code,
                    bookings_created,
                )
    return bookings_created


def clear_waitlist_due_before_reference(now_dt=None) -> int:
    """
    Clear waitlist queues for equipments that are within the 60-minute window before
    their configured slot_window_reference_weekday/time.

    Returns:
        Number of waitlist entries deleted across all equipments.
    """
    now = timezone.localtime(now_dt or timezone.now())
    deleted_total = 0
    equipments = Equipment.objects.filter(waitlist_queue_depth__gt=0).exclude(
        slot_window_reference_weekday__isnull=True
    ).exclude(
        slot_window_reference_time__isnull=True
    )

    for equipment in equipments:
        try:
            ref_weekday = int(equipment.slot_window_reference_weekday)
            ref_time = equipment.slot_window_reference_time
            week_monday = now.date() - timedelta(days=now.weekday())
            ref_date = week_monday + timedelta(days=ref_weekday)
            ref_naive = datetime.combine(ref_date, ref_time)
            ref_datetime = timezone.make_aware(ref_naive, timezone.get_current_timezone())
            cutoff = ref_datetime - timedelta(minutes=60)
            if cutoff <= now < ref_datetime:
                # Attempt FCFS confirmations while slots exist before clearing remaining entries.
                try:
                    notify_waitlist_slots_available(equipment)
                except Exception:
                    logger.exception(
                        "Pre-reference waitlist clear: notify_waitlist_slots_available failed for equipment %s",
                        getattr(equipment, "code", equipment.pk),
                    )
                deleted, _ = WaitlistEntry.objects.filter(equipment=equipment).delete()
                if deleted:
                    deleted_total += deleted
                    logger.info(
                        "Cleared %d waitlist entr(y/ies) for equipment %s at pre-reference cutoff.",
                        deleted,
                        getattr(equipment, "code", equipment.pk),
                    )
        except Exception as e:
            logger.warning(
                "Failed pre-reference waitlist clear for equipment %s: %s",
                getattr(equipment, "code", equipment.pk),
                e,
            )
    return deleted_total
