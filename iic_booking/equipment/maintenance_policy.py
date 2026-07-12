"""
When equipment is marked Under Maintenance (REPAIR/INACTIVE), affected same-day bookings
enter a special policy: cancel anytime (refund), reschedule only after equipment is operational
(with optional extra week in slots API), auto-cancel at slot-window deadline if undecided.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def is_equipment_under_maintenance_status(status: str | None) -> bool:
    from .models import EquipmentStatus

    s = (status or "").strip().upper()
    return s in (EquipmentStatus.REPAIR, EquipmentStatus.INACTIVE)


def is_equipment_operational_status(status: str | None) -> bool:
    from .models import EquipmentStatus

    return (status or "").strip() == EquipmentStatus.ACTIVE


def released_slot_status_after_booking_freed(equipment) -> str:
    """
    When a booking or hold is cancelled/refunded and its DailySlot rows are freed,
    use this status so empty slots stay aligned with equipment lifecycle.

    If the equipment is still under maintenance, freed slots must not become AVAILABLE.
    """
    from .models import SlotStatus

    if is_equipment_under_maintenance_status(getattr(equipment, "status", None)):
        return SlotStatus.UNDER_MAINTENANCE
    return SlotStatus.AVAILABLE


def effective_slot_status_when_freeing_disruption_booking(booking) -> str:
    """
    When refunding or auto-cancelling under the disruption policy, honor the originally
    intended slot marking (under maintenance vs operator absent).
    """
    from .models import SlotStatus

    target = getattr(booking, "disruption_release_slot_status", None)
    if target == SlotStatus.UNDER_MAINTENANCE:
        return SlotStatus.UNDER_MAINTENANCE
    if target == SlotStatus.OPERATOR_ABSENT:
        return SlotStatus.OPERATOR_ABSENT
    return released_slot_status_after_booking_freed(booking.equipment)


def resolve_disruption_decision_deadline(equipment, triggered_at, booking_first_start_aware):
    """Prefer next slot-window reference time + 1h; else legacy maintenance deadline."""
    from .api_views import get_disruption_choice_deadline_at

    d = get_disruption_choice_deadline_at(triggered_at, equipment)
    if d is not None:
        return d
    return compute_maintenance_decision_deadline(equipment, triggered_at, booking_first_start_aware)


def clear_disruption_policy_fields(booking) -> None:
    booking.maintenance_disruption_flag = False
    booking.maintenance_decision_deadline_at = None
    booking.maintenance_reschedule_extra_week = False
    booking.maintenance_operational_marked_at = None
    booking.disruption_kind = None
    booking.disruption_reason = None
    booking.disruption_release_slot_status = None
    booking.quota_period_anchor_at = None


def _booking_first_slot_start(booking) -> timezone.datetime | None:
    slots = list(booking.daily_slots.all()) if hasattr(booking, "daily_slots") else []
    with_start = [s for s in slots if getattr(s, "start_datetime", None)]
    if not with_start:
        return None
    return min(with_start, key=lambda s: s.start_datetime).start_datetime


def compute_maintenance_decision_deadline(equipment, maintenance_at, booking_first_start_aware):
    """
    Earlier of: next slot-window decision instant (15 min before reference; see urgent helper)
    and (first slot start − 15 min). At least a few minutes after maintenance_at.
    """
    from .api_views import get_slot_window_deadline_for_request

    cap = booking_first_start_aware - timedelta(minutes=15)
    sw = get_slot_window_deadline_for_request(maintenance_at, equipment)
    candidates = [cap]
    if sw is not None:
        candidates.append(sw)
    deadline = min(candidates)
    min_grace = maintenance_at + timedelta(minutes=5)
    deadline = max(deadline, min_grace)
    if deadline > cap:
        deadline = cap
    return deadline


def get_affected_booking_ids_for_equipment_today(equipment) -> set[int]:
    """Bookings with a slot today that is in progress or still in the future (same calendar day)."""
    from .models import BookingStatus, DailySlot

    now = timezone.now()
    today = timezone.localtime(now).date()
    qs = (
        DailySlot.objects.filter(
            slot_master__equipment_id=equipment.equipment_id,
            date=today,
            booking_id__isnull=False,
            booking__status__in=[BookingStatus.BOOKED, BookingStatus.PENDING],
        )
        .select_related("booking")
    )
    ids: set[int] = set()
    for slot in qs:
        if not slot.booking_id:
            continue
        st = slot.start_datetime
        en = slot.end_datetime
        if not st:
            continue
        if st <= now < (en or st):
            ids.add(slot.booking_id)
        elif st > now:
            ids.add(slot.booking_id)
    return ids


def _send_maintenance_disruption_email(booking, deadline_at) -> None:
    from iic_booking.communication.utils import booking_display_id_for_email

    user = booking.user
    if not user or not getattr(user, "email", None):
        return
    eq = booking.equipment
    eq_name = (getattr(eq, "name", None) or getattr(eq, "code", None) or "Equipment") if eq else "Equipment"
    bid = booking_display_id_for_email(booking)
    dl = timezone.localtime(deadline_at).strftime("%Y-%m-%d %H:%M %Z") if deadline_at else "—"
    subject = f"Technical issue — {eq_name} (booking {bid})"
    body = (
        f"Dear {getattr(user, 'name', None) or 'user'},\n\n"
        f"We are facing technical issues with {eq_name}. Your slot could not be completed as scheduled "
        f"because the equipment is under maintenance.\n\n"
        f"Please choose one of the following in My Bookings (Actions):\n"
        f"(a) Cancel booking — full refund to your wallet.\n"
        f"(b) Reschedule — pick a new time once the equipment is operational; we will email you when that happens. "
        f"Reschedule will then include an additional week in the booking calendar where applicable.\n\n"
        f"If you take no action before the decision deadline ({dl}), your booking will be treated as a refund request: "
        f"it will be cancelled with a full refund and the slot(s) released per our policy.\n\n"
        f"— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
    )
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("maintenance disruption email failed for booking %s: %s", booking.booking_id, e)


def _send_equipment_operational_email(booking) -> None:
    from iic_booking.communication.utils import booking_display_id_for_email
    from iic_booking.communication.utils import get_frontend_absolute_url

    user = booking.user
    if not user or not getattr(user, "email", None):
        return
    eq = booking.equipment
    eq_name = (getattr(eq, "name", None) or getattr(eq, "code", None) or "Equipment") if eq else "Equipment"
    bid = booking_display_id_for_email(booking)
    my_bookings = get_frontend_absolute_url("/dashboard/my-bookings")
    subject = f"{eq_name} is operational — you may reschedule (booking {bid})"
    body = (
        f"Dear {getattr(user, 'name', None) or 'user'},\n\n"
        f"{eq_name} is operational again. You may reschedule your booking from My Bookings. "
        f"The calendar includes one additional week of availability for this reschedule.\n\n"
        f"{my_bookings}\n\n"
        f"— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
    )
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("operational email failed for booking %s: %s", booking.booking_id, e)


def _send_auto_cancel_email(booking) -> None:
    from iic_booking.communication.utils import booking_display_id_for_email

    user = booking.user
    if not user or not getattr(user, "email", None):
        return
    eq = booking.equipment
    eq_name = (getattr(eq, "name", None) or getattr(eq, "code", None) or "Equipment") if eq else "Equipment"
    bid = booking_display_id_for_email(booking)
    subject = f"Booking {bid} cancelled — full refund ({eq_name})"
    body = (
        f"Dear {getattr(user, 'name', None) or 'user'},\n\n"
        f"Your booking {bid} on {eq_name} was automatically cancelled because no choice (refund or reschedule) "
        f"was received before the disruption decision deadline. A full refund has been issued to your wallet where applicable.\n\n"
        f"— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
    )
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("auto-cancel email failed for booking %s: %s", booking.booking_id, e)


def _send_operator_absent_disruption_email(booking, deadline_at) -> None:
    """
    Similar to maintenance disruption email, but for OPERATOR_ABSENT slots.
    Cancel is available any time for full refund; reschedule is available immediately (equipment is operational).
    """
    from iic_booking.communication.utils import booking_display_id_for_email

    user = booking.user
    if not user or not getattr(user, "email", None):
        return
    eq = booking.equipment
    eq_name = (getattr(eq, "name", None) or getattr(eq, "code", None) or "Equipment") if eq else "Equipment"
    bid = booking_display_id_for_email(booking)
    dl = timezone.localtime(deadline_at).strftime("%Y-%m-%d %H:%M %Z") if deadline_at else "—"
    subject = f"Operator unavailable — {eq_name} (booking {bid})"
    body = (
        f"Dear {getattr(user, 'name', None) or 'user'},\n\n"
        f"The operator is unavailable for {eq_name}. Your slot could not be completed as scheduled.\n\n"
        f"Please choose one of the following in My Bookings (Actions):\n"
        f"(a) Cancel booking — full refund to your wallet.\n"
        f"(b) Reschedule — pick a new time from My Bookings.\n\n"
        f"If you take no action before the decision deadline ({dl}), your booking will be treated as a refund request: "
        f"it will be cancelled with a full refund and the slot(s) released per our policy.\n\n"
        f"— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
    )
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("operator absent disruption email failed for booking %s: %s", booking.booking_id, e)


def _send_other_disruption_email(booking, deadline_at, reason: str) -> None:
    """
    Similar to operator-absent disruption email, but includes a staff-provided reason.
    Cancel (refund) is available any time; reschedule is available immediately (equipment is operational).
    """
    from iic_booking.communication.utils import booking_display_id_for_email

    user = booking.user
    if not user or not getattr(user, "email", None):
        return
    eq = booking.equipment
    eq_name = (getattr(eq, "name", None) or getattr(eq, "code", None) or "Equipment") if eq else "Equipment"
    bid = booking_display_id_for_email(booking)
    dl = timezone.localtime(deadline_at).strftime("%Y-%m-%d %H:%M %Z") if deadline_at else "—"
    reason_clean = (reason or "").strip()
    subject = f"Disruption — {eq_name} (booking {bid})"
    body = (
        f"Dear {getattr(user, 'name', None) or 'user'},\n\n"
        f"Your slot could not be completed as scheduled for {eq_name}.\n\n"
        f"Reason: {reason_clean or '—'}\n\n"
        f"Please choose one of the following in My Bookings (Actions):\n"
        f"(a) Cancel booking — full refund to your wallet.\n"
        f"(b) Reschedule — pick a new time from My Bookings.\n\n"
        f"If you take no action before the decision deadline ({dl}), your booking will be treated as a refund request: "
        f"it will be cancelled with a full refund and the slot(s) released per our policy.\n\n"
        f"— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
    )
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("other disruption email failed for booking %s: %s", booking.booking_id, e)


def apply_maintenance_disruption_for_booking_manually(booking, *, notes: str = "") -> None:
    """
    Admin/OIC: put a BOOKED/PENDING booking into disruption-pending (awaiting refund vs reschedule).
    User is emailed; status becomes DISRUPTION_PENDING until they act or the deadline passes.
    """
    from .models import BookingDisruptionKind, BookingStatus, SlotStatus

    if booking.status not in (BookingStatus.BOOKED, BookingStatus.PENDING):
        raise ValueError(
            f"Cannot flag booking for maintenance disruption with status '{booking.status}'. "
            "Only BOOKED or PENDING bookings are allowed."
        )
    if booking.status == BookingStatus.DISRUPTION_PENDING or booking.maintenance_disruption_flag:
        raise ValueError("This booking is already in the disruption (awaiting choice) workflow.")

    equipment = booking.equipment
    maintenance_at = timezone.now()
    first_start = _booking_first_slot_start(booking)
    if not first_start:
        raise ValueError("Booking has no slots to anchor the decision deadline.")
    if timezone.is_naive(first_start):
        first_start = timezone.make_aware(first_start)

    deadline = resolve_disruption_decision_deadline(equipment, maintenance_at, first_start)

    extra_notes = (notes or "").strip()
    update_fields = [
        "status",
        "disruption_kind",
        "disruption_release_slot_status",
        "quota_period_anchor_at",
        "maintenance_disruption_flag",
        "maintenance_decision_deadline_at",
        "maintenance_reschedule_extra_week",
        "maintenance_operational_marked_at",
    ]
    if extra_notes:
        tag = "[Under maintenance — Admin/OIC]"
        booking.notes = f"{booking.notes or ''}\n{tag}: {extra_notes}".strip()
        update_fields.append("notes")

    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.MAINTENANCE
    booking.disruption_release_slot_status = SlotStatus.UNDER_MAINTENANCE
    booking.quota_period_anchor_at = first_start
    booking.maintenance_disruption_flag = True
    booking.maintenance_decision_deadline_at = deadline
    # Allow one additional week navigation immediately while awaiting the user's choice.
    # Reschedule may still be blocked by equipment status until it becomes operational.
    booking.maintenance_reschedule_extra_week = True
    booking.maintenance_operational_marked_at = None
    booking.save(update_fields=update_fields)

    _send_maintenance_disruption_email(booking, deadline)

    # Manual (booking-details) disruption: send an "operational" reschedule email 5 minutes later
    # if the booking is still awaiting the user's decision.
    try:
        from .tasks import send_manual_disruption_operational_followup

        send_manual_disruption_operational_followup.apply_async(args=[booking.booking_id], countdown=300)
    except Exception:
        logger.exception("Failed to schedule manual disruption operational follow-up for booking %s", booking.booking_id)


def apply_operator_absent_disruption_for_booking(booking, *, triggered_at=None) -> None:
    """
    Slot/admin path: booking enters disruption-pending (operator absent) with email and deadline.
    """
    from .models import BookingDisruptionKind, BookingStatus, SlotStatus

    if booking.status not in (BookingStatus.BOOKED, BookingStatus.PENDING):
        return
    if booking.status == BookingStatus.DISRUPTION_PENDING:
        return
    triggered_at = triggered_at or timezone.now()
    first_start = _booking_first_slot_start(booking)
    if not first_start:
        return
    if timezone.is_naive(first_start):
        first_start = timezone.make_aware(first_start)
    equipment = booking.equipment
    deadline = resolve_disruption_decision_deadline(equipment, triggered_at, first_start)
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.disruption_release_slot_status = SlotStatus.OPERATOR_ABSENT
    booking.maintenance_disruption_flag = True
    booking.maintenance_decision_deadline_at = deadline
    booking.maintenance_reschedule_extra_week = True
    booking.save(
        update_fields=[
            "status",
            "disruption_kind",
            "disruption_release_slot_status",
            "quota_period_anchor_at",
            "maintenance_disruption_flag",
            "maintenance_decision_deadline_at",
            "maintenance_reschedule_extra_week",
        ]
    )
    _send_operator_absent_disruption_email(booking, deadline)
    try:
        from .tasks import send_manual_disruption_operational_followup

        send_manual_disruption_operational_followup.apply_async(args=[booking.booking_id], countdown=300)
    except Exception:
        logger.exception(
            "Failed to schedule disruption operational follow-up for operator-absent booking %s",
            booking.booking_id,
        )


def apply_operator_disruption_pending_from_staff(booking, *, notes: str = "") -> None:
    """
    Operator/manager/admin 'Operator Unavailable' on a booking: same disruption-pending policy as slot-based path.
    No immediate refund.
    """
    from .models import BookingDisruptionKind, BookingStatus, SlotStatus

    if booking.status not in (BookingStatus.BOOKED, BookingStatus.PENDING):
        raise ValueError(
            f"Cannot mark operator unavailable for booking status '{booking.status}'. "
            "Only BOOKED or PENDING bookings are allowed."
        )
    if booking.status == BookingStatus.DISRUPTION_PENDING or booking.maintenance_disruption_flag:
        raise ValueError("This booking is already in the disruption (awaiting choice) workflow.")

    triggered_at = timezone.now()
    first_start = _booking_first_slot_start(booking)
    if not first_start:
        raise ValueError("Booking has no slots to anchor the decision deadline.")
    if timezone.is_naive(first_start):
        first_start = timezone.make_aware(first_start)
    equipment = booking.equipment
    deadline = resolve_disruption_decision_deadline(equipment, triggered_at, first_start)

    extra_notes = (notes or "").strip()
    update_fields = [
        "status",
        "disruption_kind",
        "disruption_release_slot_status",
        "quota_period_anchor_at",
        "maintenance_disruption_flag",
        "maintenance_decision_deadline_at",
        "maintenance_reschedule_extra_week",
        "notes",
    ]
    if extra_notes:
        booking.notes = f"{booking.notes or ''}\n[Operator Unavailable]: {extra_notes}".strip()
    else:
        update_fields.remove("notes")

    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.disruption_release_slot_status = SlotStatus.OPERATOR_ABSENT
    booking.quota_period_anchor_at = first_start
    booking.maintenance_disruption_flag = True
    booking.maintenance_decision_deadline_at = deadline
    booking.maintenance_reschedule_extra_week = True
    booking.save(update_fields=update_fields)
    _send_operator_absent_disruption_email(booking, deadline)
    try:
        from .tasks import send_manual_disruption_operational_followup

        send_manual_disruption_operational_followup.apply_async(args=[booking.booking_id], countdown=300)
    except Exception:
        logger.exception(
            "Failed to schedule disruption operational follow-up for staff operator-unavailable booking %s",
            booking.booking_id,
        )


def apply_other_disruption_for_booking_manually(booking, *, reason: str) -> None:
    """
    Admin/OIC: put a BOOKED/PENDING booking into disruption-pending with kind OTHER_DISRUPTION.
    Reason is required and is emailed to the user. Policy matches operator-absent disruption.
    """
    from .models import BookingDisruptionKind, BookingStatus, SlotStatus

    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise ValueError("Reason is required for Other Disruption.")

    if booking.status not in (BookingStatus.BOOKED, BookingStatus.PENDING):
        raise ValueError(
            f"Cannot flag booking for other disruption with status '{booking.status}'. "
            "Only BOOKED or PENDING bookings are allowed."
        )
    if booking.status == BookingStatus.DISRUPTION_PENDING or booking.maintenance_disruption_flag:
        raise ValueError("This booking is already in the disruption (awaiting choice) workflow.")

    triggered_at = timezone.now()
    first_start = _booking_first_slot_start(booking)
    if not first_start:
        raise ValueError("Booking has no slots to anchor the decision deadline.")
    if timezone.is_naive(first_start):
        first_start = timezone.make_aware(first_start)
    equipment = booking.equipment
    deadline = resolve_disruption_decision_deadline(equipment, triggered_at, first_start)

    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OTHER_DISRUPTION
    booking.disruption_reason = reason_clean
    booking.disruption_release_slot_status = SlotStatus.OPERATOR_ABSENT
    booking.quota_period_anchor_at = first_start
    booking.maintenance_disruption_flag = True
    booking.maintenance_decision_deadline_at = deadline
    booking.maintenance_reschedule_extra_week = True
    booking.save(
        update_fields=[
            "status",
            "disruption_kind",
            "disruption_reason",
            "disruption_release_slot_status",
            "quota_period_anchor_at",
            "maintenance_disruption_flag",
            "maintenance_decision_deadline_at",
            "maintenance_reschedule_extra_week",
        ]
    )
    _send_other_disruption_email(booking, deadline, reason_clean)
    try:
        from .tasks import send_manual_disruption_operational_followup

        send_manual_disruption_operational_followup.apply_async(args=[booking.booking_id], countdown=300)
    except Exception:
        logger.exception(
            "Failed to schedule disruption operational follow-up for other-disruption booking %s",
            booking.booking_id,
        )


def apply_when_equipment_marked_under_maintenance(equipment) -> int:
    """Flag affected bookings, set deadlines, send disruption emails. Returns count updated."""
    from .models import Booking, BookingDisruptionKind, BookingStatus, DailySlot, SlotStatus

    # Make slot statuses match the equipment state immediately.
    # Otherwise users may see future days as AVAILABLE until the next scheduled sweep runs.
    now = timezone.now()
    today = timezone.localtime(now).date()
    DailySlot.objects.filter(
        slot_master__equipment=equipment,
        date__gte=today,
        status=SlotStatus.AVAILABLE,
    ).update(status=SlotStatus.UNDER_MAINTENANCE)

    ids = get_affected_booking_ids_for_equipment_today(equipment)
    if not ids:
        return 0
    maintenance_at = now
    count = 0
    for bid in ids:
        try:
            booking = (
                Booking.objects.select_related("equipment", "user")
                .prefetch_related("daily_slots")
                .get(pk=bid)
            )
        except Booking.DoesNotExist:
            continue
        if booking.status == BookingStatus.DISRUPTION_PENDING:
            continue
        if booking.status not in (BookingStatus.BOOKED, BookingStatus.PENDING):
            continue
        first_start = _booking_first_slot_start(booking)
        if not first_start:
            continue
        if timezone.is_naive(first_start):
            first_start = timezone.make_aware(first_start)
        deadline = resolve_disruption_decision_deadline(equipment, maintenance_at, first_start)
        booking.status = BookingStatus.DISRUPTION_PENDING
        booking.disruption_kind = BookingDisruptionKind.MAINTENANCE
        booking.disruption_release_slot_status = SlotStatus.UNDER_MAINTENANCE
        booking.maintenance_disruption_flag = True
        booking.maintenance_decision_deadline_at = deadline
        # Allow one additional week navigation immediately while awaiting the user's choice.
        # Reschedule may still be blocked by equipment status until it becomes operational.
        booking.maintenance_reschedule_extra_week = True
        booking.maintenance_operational_marked_at = None
        booking.save(
            update_fields=[
                "status",
                "disruption_kind",
                "disruption_release_slot_status",
                "quota_period_anchor_at",
                "maintenance_disruption_flag",
                "maintenance_decision_deadline_at",
                "maintenance_reschedule_extra_week",
                "maintenance_operational_marked_at",
            ]
        )
        _send_maintenance_disruption_email(booking, deadline)
        try:
            from .tasks import send_manual_disruption_operational_followup

            send_manual_disruption_operational_followup.apply_async(args=[booking.booking_id], countdown=300)
        except Exception:
            logger.exception(
                "Failed to schedule disruption operational follow-up for maintenance booking %s",
                booking.booking_id,
            )
        count += 1
    return count


def apply_when_equipment_becomes_operational(equipment) -> int:
    """Unlock maintenance reschedule (+1 week) and notify users."""
    from django.db.models import Q

    from .models import Booking, BookingDisruptionKind, BookingStatus, DailySlot, SlotStatus

    # Restore previously under-maintenance slots to AVAILABLE for booking.
    # Keep booked/held slots untouched (they should already be BOOKED/HOLD/etc).
    now = timezone.now()
    today = timezone.localtime(now).date()
    freed_qs = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        date__gte=today,
        status=SlotStatus.UNDER_MAINTENANCE,
        booking_id__isnull=True,
    )
    freed_slot_ids = list(freed_qs.values_list("id", flat=True))
    freed_qs.update(status=SlotStatus.AVAILABLE)

    qs = (
        Booking.objects.filter(
            equipment_id=equipment.equipment_id,
            maintenance_disruption_flag=True,
        )
        .exclude(disruption_kind=BookingDisruptionKind.OPERATOR_ABSENT)
        .filter(
            Q(status__in=[BookingStatus.BOOKED, BookingStatus.PENDING])
            | Q(status=BookingStatus.DISRUPTION_PENDING)
        )
        .select_related("user", "equipment")
    )
    n = 0
    marked_at = timezone.now()
    for booking in qs:
        booking.maintenance_reschedule_extra_week = True
        booking.maintenance_operational_marked_at = marked_at
        booking.save(
            update_fields=["maintenance_reschedule_extra_week", "maintenance_operational_marked_at"]
        )
        _send_equipment_operational_email(booking)
        n += 1

    if freed_slot_ids:
        try:
            from .waitlist import notify_waitlist_slots_available

            notify_waitlist_slots_available(
                equipment,
                preferred_slot_ids=freed_slot_ids,
                respect_reschedule_threshold=False,
            )
        except Exception:
            logger.exception(
                "Failed to run waitlist auto-book after equipment %s became operational",
                getattr(equipment, "code", equipment.pk),
            )
    return n


def on_equipment_status_changed(equipment, old_status: str | None, new_status: str | None) -> None:
    if old_status == new_status:
        return
    if is_equipment_under_maintenance_status(new_status) and not is_equipment_under_maintenance_status(old_status):
        try:
            n = apply_when_equipment_marked_under_maintenance(equipment)
            logger.info(
                "maintenance_policy: equipment %s marked under maintenance; affected bookings=%s",
                equipment.equipment_id,
                n,
            )
        except Exception:
            logger.exception("apply_when_equipment_marked_under_maintenance failed for equipment %s", equipment.pk)
    elif is_equipment_operational_status(new_status) and is_equipment_under_maintenance_status(old_status):
        try:
            n = apply_when_equipment_becomes_operational(equipment)
            logger.info(
                "maintenance_policy: equipment %s operational; maintenance reschedule unlock emails=%s",
                equipment.equipment_id,
                n,
            )
        except Exception:
            logger.exception("apply_when_equipment_becomes_operational failed for equipment %s", equipment.pk)


def auto_cancel_expired_maintenance_bookings() -> int:
    """Auto-cancel disruption bookings past decision deadline with full refund (default to refund if no action)."""
    from .models import Booking, BookingDisruptionKind, BookingStatus, BookingEventType
    from .booking_events import create_booking_event
    from iic_booking.users.repositories.wallet_repository import WalletRepository
    from iic_booking.equipment.api_views import (
        _reverse_reward_points_for_booking,
        _student_booking_description_suffix,
    )

    now = timezone.now()
    qs = (
        Booking.objects.filter(
            maintenance_disruption_flag=True,
            maintenance_decision_deadline_at__lte=now,
            status__in=[BookingStatus.BOOKED, BookingStatus.PENDING, BookingStatus.DISRUPTION_PENDING],
        )
        .select_related("user", "equipment")
    )
    cancelled = 0
    for booking in qs:
        try:
            released_slot_ids = list(booking.daily_slots.values_list("id", flat=True))
            with transaction.atomic():
                previous_status = booking.status
                slot_status = effective_slot_status_when_freeing_disruption_booking(booking)
                booking.daily_slots.update(
                    booking=None,
                    status=slot_status,
                )
                refund_target, _ = WalletRepository.get_booking_wallet_target(
                    booking.user, getattr(booking.equipment, "internal_department", None)
                )
                if refund_target:
                    refund_amount = Decimal(str(booking.total_charge or "0"))
                    from iic_booking.communication.utils import booking_display_id_for_email

                    desc = (
                        f"Refund for auto-cancelled Booking {booking_display_id_for_email(booking)} "
                        f"(maintenance policy deadline) — {(getattr(booking.equipment, 'name', None) or getattr(booking.equipment, 'code', None) or '')}"
                    )
                    desc += _student_booking_description_suffix(refund_target, booking.user)
                    if refund_amount > 0:
                        refund_target.credit(
                            amount=refund_amount,
                            description=desc,
                            related_user=booking.user,
                        )
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
                    _reverse_reward_points_for_booking(
                        booking, booking.user, "Reward points reversed for maintenance auto-cancel refund"
                    )
                else:
                    booking.status = BookingStatus.CANCELLED
                    new_status = BookingStatus.CANCELLED
                    event_type = BookingEventType.CANCELLED
                clear_disruption_policy_fields(booking)
                booking.notes = (
                    (booking.notes or "")
                    + "\n[Auto-cancelled: maintenance policy deadline with no user action.]"
                ).strip()
                booking.save()
                create_booking_event(
                    booking=booking,
                    event_type=event_type,
                    previous_status=previous_status,
                    new_status=new_status,
                    comment="Auto-cancelled: maintenance disruption policy deadline.",
                    created_by=None,
                    send_notification=False,
                )
            try:
                from .waitlist import notify_waitlist_slots_available

                notify_waitlist_slots_available(
                    booking.equipment,
                    preferred_slot_ids=released_slot_ids,
                    respect_reschedule_threshold=True,
                )
            except Exception:
                logger.warning(
                    "Failed to notify waitlist after maintenance auto-cancel for booking %s",
                    booking.booking_id,
                    exc_info=True,
                )
            _send_auto_cancel_email(booking)
            cancelled += 1
        except Exception:
            logger.exception("auto_cancel maintenance booking failed for %s", booking.booking_id)
    return cancelled


def daily_under_maintenance_sweep_for_today() -> dict[str, int]:
    """
    Run daily (recommended: 08:15 local time) while equipments remain under maintenance.
    For each under-maintenance equipment:
      1) Mark today's AVAILABLE slots as UNDER_MAINTENANCE.
      2) Apply/refresh disruption policy for today's in-progress/future bookings and send email.
    """
    from .models import Equipment, DailySlot, SlotStatus

    now = timezone.now()
    today = timezone.localtime(now).date()
    equipments = Equipment.objects.all().only("equipment_id", "status")
    slots_marked = 0
    bookings_notified = 0

    for eq in equipments:
        if not is_equipment_under_maintenance_status(getattr(eq, "status", None)):
            continue

        # Keep slot statuses aligned with equipment state: all today's available slots become UNDER_MAINTENANCE.
        updated = DailySlot.objects.filter(
            slot_master__equipment_id=eq.equipment_id,
            date=today,
            status=SlotStatus.AVAILABLE,
        ).update(status=SlotStatus.UNDER_MAINTENANCE)
        slots_marked += int(updated or 0)

        # Notify today's affected bookings (in-progress/future) and ensure maintenance flags/deadlines are set.
        bookings_notified += apply_when_equipment_marked_under_maintenance(eq)

    return {
        "equipments_checked": equipments.count(),
        "slots_marked_under_maintenance": slots_marked,
        "bookings_notified": bookings_notified,
    }
