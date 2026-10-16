"""
When equipment is marked Under Maintenance (REPAIR/INACTIVE), affected same-day bookings
enter a special policy: cancel anytime (refund), reschedule only after equipment is operational
(with optional extra week in slots API); undecided after DISRUPTION_DECISION_DAYS, the booking is
marked Refunded with a full refund.
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
    """REPAIR / INACTIVE, and the legacy MAINTENANCE ("Maintenance Scheduled", no longer offered) still on old rows."""
    from .models import EquipmentStatus

    s = (status or "").strip().upper()
    return s in (EquipmentStatus.REPAIR, EquipmentStatus.INACTIVE, EquipmentStatus.MAINTENANCE)


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


DISRUPTION_DECISION_DAYS = 5


def resolve_disruption_decision_deadline(equipment, triggered_at, booking_first_start_aware):
    """The user has DISRUPTION_DECISION_DAYS from the disruption mark (OIC / staff / system) to choose
    refund or reschedule; undecided bookings are then refunded in full (``auto_cancel_expired_maintenance_bookings``)."""
    return triggered_at + timedelta(days=DISRUPTION_DECISION_DAYS)


def clear_disruption_policy_fields(booking, *, keep_quota_anchor: bool = False) -> None:
    """``keep_quota_anchor``: the booking was rescheduled after a disruption and keeps counting
    in its original quota period (the user's quota is unchanged by the move)."""
    booking.maintenance_disruption_flag = False
    booking.maintenance_decision_deadline_at = None
    booking.maintenance_reschedule_extra_week = False
    booking.maintenance_operational_marked_at = None
    booking.disruption_kind = None
    booking.disruption_reason = None
    booking.disruption_release_slot_status = None
    if not keep_quota_anchor:
        booking.quota_period_anchor_at = None


def _disruption_quota_anchor(booking, first_start):
    """An earlier anchor (booking already moved by a disruption or staff) wins over the current slots."""
    return getattr(booking, "quota_period_anchor_at", None) or first_start


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


def _fbr_block(booking) -> str:
    from .fbr_email import fbr_text_line

    line = fbr_text_line(booking)
    return f"{line}\n\n" if line else ""


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
        f"it will be marked Refunded, the full amount refunded to your wallet and the slot(s) released per our policy.\n\n"
        f"{_fbr_block(booking)}— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
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
        f"{_fbr_block(booking)}— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
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
    subject = f"Booking {bid} refunded — full refund ({eq_name})"
    body = (
        f"Dear {getattr(user, 'name', None) or 'user'},\n\n"
        f"Your booking {bid} on {eq_name} has been marked Refunded because no choice (refund or reschedule) "
        f"was received within {DISRUPTION_DECISION_DAYS} days of the disruption. "
        f"A full refund has been issued to your wallet where applicable.\n\n"
        f"{_fbr_block(booking)}— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
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
        f"it will be marked Refunded, the full amount refunded to your wallet and the slot(s) released per our policy.\n\n"
        f"{_fbr_block(booking)}— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
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
    subject = f"Analysis Not Possible — {eq_name} (booking {bid})"
    body = (
        f"Dear {getattr(user, 'name', None) or 'user'},\n\n"
        f"Analysis could not be completed for your booking on {eq_name}.\n\n"
        f"Reason: {reason_clean or '—'}\n\n"
        f"Please choose one of the following in My Bookings (Actions):\n"
        f"(a) Cancel booking — full refund to your wallet.\n"
        f"(b) Reschedule — pick a new time from My Bookings.\n\n"
        f"If you take no action before the decision deadline ({dl}), your booking will be treated as a refund request: "
        f"it will be marked Refunded, the full amount refunded to your wallet and the slot(s) released per our policy.\n\n"
        f"{_fbr_block(booking)}— {getattr(settings, 'SITE_NAME', 'IIC Booking')}"
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


def _record_disruption_entry(booking, previous_status, deadline, *, reason: str = "") -> None:
    """History entry for a booking that now awaits the user's refund / reschedule choice.

    Staff paths run in their request (the signed-in OIC / admin / operator is the actor); jobs and scripts
    record the System.
    """
    from .booking_audit import AUDIT_KEY, DISRUPTION_KIND_REASONS, REASON_LABELS, current_actor, reason_fields
    from .booking_events import create_booking_event
    from .models import BookingEventType

    code = DISRUPTION_KIND_REASONS.get(str(booking.disruption_kind or ""), "")
    label = REASON_LABELS.get(code, "Lab disruption")
    reason_clean = (reason or "").strip()
    deadline_text = timezone.localtime(deadline).strftime("%d %b %Y %H:%M") if deadline else "—"
    comment = (
        f"Marked {label}."
        + (f" Reason: {reason_clean}." if reason_clean else "")
        + f" Awaiting the user's choice (reschedule or full refund) until {deadline_text}; "
        "after that the booking is refunded in full automatically."
    )
    actor = current_actor()
    try:
        with transaction.atomic():
            create_booking_event(
                booking=booking,
                event_type=BookingEventType.STATUS_CHANGED,
                created_by=actor,
                system_actor=actor is None,
                previous_status=previous_status,
                new_status=booking.status,
                comment=comment,
                metadata={
                    AUDIT_KEY: reason_fields(code, reason_clean, disruption_kind=booking.disruption_kind),
                    "decision_deadline_at": deadline.isoformat() if deadline else None,
                },
                send_notification=False,
            )
    except Exception:
        logger.exception("Could not record the disruption of booking %s", booking.booking_id)


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

    previous_status = booking.status
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.MAINTENANCE
    booking.disruption_release_slot_status = SlotStatus.UNDER_MAINTENANCE
    booking.quota_period_anchor_at = _disruption_quota_anchor(booking, first_start)
    booking.maintenance_disruption_flag = True
    booking.maintenance_decision_deadline_at = deadline
    # Allow one additional week navigation immediately while awaiting the user's choice.
    # Reschedule may still be blocked by equipment status until it becomes operational.
    booking.maintenance_reschedule_extra_week = True
    booking.maintenance_operational_marked_at = None
    booking.save(update_fields=update_fields)
    _record_disruption_entry(booking, previous_status, deadline, reason=extra_notes)

    _send_maintenance_disruption_email(booking, deadline)

    # Manual (booking-details) disruption: send an "operational" reschedule email 5 minutes later
    # if the booking is still awaiting the user's decision.
    try:
        from .tasks import send_manual_disruption_operational_followup

        send_manual_disruption_operational_followup.apply_async(args=[booking.booking_id], countdown=300)
    except Exception:
        logger.exception("Failed to schedule manual disruption operational follow-up for booking %s", booking.booking_id)


def apply_operator_absent_disruption_for_booking(booking, *, triggered_at=None, reason: str = "") -> None:
    """
    Slot/admin path: booking enters disruption-pending (operator absent) with email and deadline.
    ``reason`` says why for the booking history (e.g. which safeguard marked it).
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
    previous_status = booking.status
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.disruption_release_slot_status = SlotStatus.OPERATOR_ABSENT
    booking.quota_period_anchor_at = _disruption_quota_anchor(booking, first_start)
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
    _record_disruption_entry(booking, previous_status, deadline, reason=reason)
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

    previous_status = booking.status
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.disruption_release_slot_status = SlotStatus.OPERATOR_ABSENT
    booking.quota_period_anchor_at = _disruption_quota_anchor(booking, first_start)
    booking.maintenance_disruption_flag = True
    booking.maintenance_decision_deadline_at = deadline
    booking.maintenance_reschedule_extra_week = True
    booking.save(update_fields=update_fields)
    _record_disruption_entry(booking, previous_status, deadline, reason=extra_notes)
    _send_operator_absent_disruption_email(booking, deadline)
    try:
        from .tasks import send_manual_disruption_operational_followup

        send_manual_disruption_operational_followup.apply_async(args=[booking.booking_id], countdown=300)
    except Exception:
        logger.exception(
            "Failed to schedule disruption operational follow-up for staff operator-unavailable booking %s",
            booking.booking_id,
        )


ANALYSIS_NOT_POSSIBLE_ROLE_ERROR = (
    "Only Admin, Department Administrator, Officer in Charge, or Lab Operator "
    "can flag a booking as Analysis Not Possible."
)


def analysis_not_possible_scope_error(user, booking) -> str | None:
    """
    Who may flag a booking as Analysis Not Possible: Main Admin (any), Department Administrator (their
    department), Officer in Charge incl. temporary OIC (equipment they manage), and Lab Operator (equipment
    they are assigned to, incl. acting cover). The same rule applies whatever the sample lifecycle stage.
    """
    from iic_booking.users.models.user_type import UserType

    from .api_views import _get_equipment_ids_for_log_access
    from .reports import get_equipment_ids_managed_by_oic

    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return None
    if ut == UserType.MANAGER:
        allowed, error = get_equipment_ids_managed_by_oic(user.id), "You do not manage this equipment."
    elif ut == UserType.DEPT_ADMIN:
        allowed, error = _get_equipment_ids_for_log_access(user), "This booking is outside your department."
    elif ut == UserType.OPERATOR:
        allowed, error = _get_equipment_ids_for_log_access(user), "You are not assigned to this equipment."
    else:
        return ANALYSIS_NOT_POSSIBLE_ROLE_ERROR
    return None if booking.equipment_id in set(allowed or []) else error


def analysis_not_possible_state_error(booking) -> str | None:
    from .models import BookingStatus

    if booking.status not in (BookingStatus.BOOKED, BookingStatus.PENDING):
        return (
            f"Cannot flag booking as Analysis Not Possible with status '{booking.status}'. "
            "Only BOOKED or PENDING bookings are allowed."
        )
    if booking.status == BookingStatus.DISRUPTION_PENDING or booking.maintenance_disruption_flag:
        return "This booking is already in the disruption (awaiting choice) workflow."
    return None


def can_flag_analysis_not_possible(user, booking) -> bool:
    return analysis_not_possible_scope_error(user, booking) is None and analysis_not_possible_state_error(booking) is None


def apply_other_disruption_for_booking_manually(booking, *, reason: str) -> None:
    """
    Staff action "Analysis Not Possible": put a BOOKED/PENDING booking into disruption-pending
    with kind OTHER_DISRUPTION. Reason is required and is emailed to the user.
    """
    from .models import BookingDisruptionKind, BookingStatus, SlotStatus

    reason_clean = (reason or "").strip()
    if not reason_clean:
        raise ValueError("Reason is required for Analysis Not Possible.")

    state_error = analysis_not_possible_state_error(booking)
    if state_error:
        raise ValueError(state_error)

    triggered_at = timezone.now()
    first_start = _booking_first_slot_start(booking)
    if not first_start:
        raise ValueError("Booking has no slots to anchor the decision deadline.")
    if timezone.is_naive(first_start):
        first_start = timezone.make_aware(first_start)
    equipment = booking.equipment
    deadline = resolve_disruption_decision_deadline(equipment, triggered_at, first_start)

    previous_status = booking.status
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OTHER_DISRUPTION
    booking.disruption_reason = reason_clean
    booking.disruption_release_slot_status = SlotStatus.OPERATOR_ABSENT
    booking.quota_period_anchor_at = _disruption_quota_anchor(booking, first_start)
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
    _record_disruption_entry(booking, previous_status, deadline, reason=reason_clean)
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
    from .models import Booking, DailySlot, SlotStatus

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
        if put_booking_on_maintenance_hold(booking, now, reason="Equipment status changed to Under Maintenance"):
            count += 1
    return count


def put_booking_on_maintenance_hold(booking, maintenance_at, *, reason: str, send_email: bool = True) -> bool:
    """A BOOKED / PENDING booking on a maintenance day awaits the user's refund or reschedule choice.

    ``send_email=False`` when the caller sends its own email (``maintenance_auto``). Returns False when the booking
    is not eligible (already awaiting a choice, other status, no slots)."""
    from .models import BookingDisruptionKind, BookingStatus, SlotStatus

    if booking.status not in (BookingStatus.BOOKED, BookingStatus.PENDING):
        return False
    first_start = _booking_first_slot_start(booking)
    if not first_start:
        return False
    if timezone.is_naive(first_start):
        first_start = timezone.make_aware(first_start)
    equipment = booking.equipment
    deadline = resolve_disruption_decision_deadline(equipment, maintenance_at, first_start)
    previous_status = booking.status
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.MAINTENANCE
    booking.disruption_release_slot_status = SlotStatus.UNDER_MAINTENANCE
    booking.quota_period_anchor_at = _disruption_quota_anchor(booking, first_start)
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
    _record_disruption_entry(booking, previous_status, deadline, reason=reason)
    if send_email:
        _send_maintenance_disruption_email(booking, deadline)
    try:
        from .tasks import send_manual_disruption_operational_followup

        send_manual_disruption_operational_followup.apply_async(args=[booking.booking_id], countdown=300)
    except Exception:
        logger.exception(
            "Failed to schedule disruption operational follow-up for maintenance booking %s",
            booking.booking_id,
        )
    return True


def apply_when_equipment_becomes_operational(equipment, *, restore_slots: bool = True) -> int:
    """Unlock maintenance reschedule (+1 week) and notify users.

    ``restore_slots=False`` when ``maintenance_auto.end_period`` already put back only the slots it converted."""
    from django.db.models import Q

    from .models import Booking, BookingDisruptionKind, BookingStatus, DailySlot, SlotStatus

    # Restore previously under-maintenance slots to AVAILABLE for booking.
    # Keep booked/held slots untouched (they should already be BOOKED/HOLD/etc).
    # Restored slots do not auto-confirm the waitlist (only booking cancellations / reschedules do).
    if restore_slots:
        today = timezone.localdate()
        DailySlot.objects.filter(
            slot_master__equipment=equipment,
            date__gte=today,
            status=SlotStatus.UNDER_MAINTENANCE,
            booking_id__isnull=True,
        ).update(status=SlotStatus.AVAILABLE)

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
    return n


def _equipment_disruption_input(equipment):
    """Who / why for the disruption log: API paths set ``equipment._disruption_input`` before saving."""
    from .disruption_service import DisruptionInput

    data = getattr(equipment, "_disruption_input", None)
    return data if isinstance(data, DisruptionInput) else DisruptionInput(source="OTHER")


def _leaves_legacy_maintenance_without_period(equipment, old_status, new_status) -> bool:
    """Legacy MAINTENANCE -> Under Maintenance: never handled by the mechanism, so it starts now."""
    from .maintenance_auto import active_period
    from .models import EquipmentStatus

    return (
        (old_status or "").strip().upper() == EquipmentStatus.MAINTENANCE
        and (new_status or "").strip().upper() != EquipmentStatus.MAINTENANCE
        and is_equipment_under_maintenance_status(new_status)
        and active_period(equipment) is None
    )


def on_equipment_status_changed(equipment, old_status: str | None, new_status: str | None) -> None:
    from .models import SlotStatus

    if old_status == new_status:
        return
    if is_equipment_under_maintenance_status(new_status) and (
        not is_equipment_under_maintenance_status(old_status)
        or _leaves_legacy_maintenance_without_period(equipment, old_status, new_status)
    ):
        from .disruption_service import record_equipment_under_maintenance, record_unrecorded_slots
        from .maintenance_auto import start_period

        data = _equipment_disruption_input(equipment)
        equipment._disruption_result = record_equipment_under_maintenance(equipment, data, bookings_affected=0)
        try:
            outcome = start_period(equipment, user=data.user)
            equipment._maintenance_outcome = outcome
            logger.info(
                "maintenance_policy: equipment %s marked under maintenance; bookings on hold=%s",
                equipment.equipment_id,
                len(outcome.bookings_on_hold),
            )
        except Exception:
            logger.exception("maintenance_auto.start_period failed for equipment %s", equipment.pk)
        record_unrecorded_slots(equipment, _upcoming_slots(equipment), data, old_status=SlotStatus.AVAILABLE)
    elif is_equipment_operational_status(new_status) and is_equipment_under_maintenance_status(old_status):
        from .disruption_service import release_slots_made_available
        from .maintenance_auto import end_period

        data = _equipment_disruption_input(equipment)
        ended = None
        try:
            ended = end_period(equipment, user=data.user)
        except Exception:
            logger.exception("maintenance_auto.end_period failed for equipment %s", equipment.pk)
        if ended is not None:
            restored_ids = list(ended.slots_restored)
        else:
            restored_ids = list(
                _upcoming_slots(equipment)
                .filter(status=SlotStatus.UNDER_MAINTENANCE, booking_id__isnull=True)
                .values_list("id", flat=True)
            )
        try:
            n = apply_when_equipment_becomes_operational(equipment, restore_slots=ended is None)
            logger.info(
                "maintenance_policy: equipment %s operational; maintenance reschedule unlock emails=%s",
                equipment.equipment_id,
                n,
            )
        except Exception:
            logger.exception("apply_when_equipment_becomes_operational failed for equipment %s", equipment.pk)
        from .disruption_service import record_equipment_operational

        release_slots_made_available(equipment, restored_ids, data)
        equipment._disruption_result = record_equipment_operational(equipment, data)


def _set_mode_status(mode, status: str, *, follows_parent: bool, data) -> None:
    mode.status = status
    mode.status_follows_parent = follows_parent
    mode._disruption_input = data
    mode._status_from_parent = True
    mode.save(update_fields=["status", "status_follows_parent", "updated_at"])


def _mode_disruption_input(base, data):
    from dataclasses import replace

    note = f"Base instrument {base.code} is Under Maintenance"
    return replace(data, reason=f"{note}: {data.reason}" if data.reason else note)


def cascade_status_to_modes(base, old_status: str | None, new_status: str | None) -> list[int]:
    """
    A base instrument Under Maintenance takes its Operational modes with it, and they come back with it.
    Each mode is saved through the normal status path, so its own bookings and slots get the full
    maintenance rules. Modes already Under Maintenance on their own are left alone.
    """
    from django.db.models import Q

    from .models import Equipment, EquipmentStatus

    if base.parent_equipment_id or old_status == new_status:
        return []
    modes = Equipment.objects.filter(parent_equipment_id=base.pk)
    changed: list[int] = []
    if is_equipment_under_maintenance_status(new_status):
        data = _mode_disruption_input(base, _equipment_disruption_input(base))
        for mode in modes.filter(Q(status=EquipmentStatus.ACTIVE) | Q(status_follows_parent=True)):
            if mode.status == new_status:
                continue
            _set_mode_status(mode, new_status, follows_parent=True, data=data)
            changed.append(mode.pk)
    elif is_equipment_operational_status(new_status) and is_equipment_under_maintenance_status(old_status):
        data = _equipment_disruption_input(base)
        for mode in modes.filter(status_follows_parent=True):
            _set_mode_status(mode, EquipmentStatus.ACTIVE, follows_parent=False, data=data)
            changed.append(mode.pk)
    if changed:
        logger.info(
            "maintenance_policy: equipment %s status %s -> %s applied to modes %s",
            base.equipment_id, old_status, new_status, changed,
        )
    return changed


def follow_base_maintenance(mode_ids) -> list[int]:
    """Operational equipment just linked as a mode of a base Under Maintenance goes Under Maintenance too."""
    from .disruption_service import DisruptionInput
    from .models import Equipment, EquipmentStatus

    changed: list[int] = []
    for mode in Equipment.objects.filter(
        pk__in=list(mode_ids), parent_equipment__isnull=False, status=EquipmentStatus.ACTIVE
    ).select_related("parent_equipment"):
        base = mode.parent_equipment
        if not is_equipment_under_maintenance_status(base.status):
            continue
        data = _mode_disruption_input(base, DisruptionInput(source="EQUIPMENT_STATUS"))
        _set_mode_status(mode, base.status, follows_parent=True, data=data)
        changed.append(mode.pk)
    return changed


def base_blocks_mode_operational(equipment, new_status: str | None, base=None) -> str:
    """Why a mode cannot be made Operational right now (its base is Under Maintenance), else ""."""
    if base is None and equipment.parent_equipment_id:
        base = equipment.parent_equipment
    if not is_equipment_operational_status(new_status) or base is None:
        return ""
    if not is_equipment_under_maintenance_status(base.status):
        return ""
    return (
        f"{equipment.code} is a mode of {base.code}, which is Under Maintenance. "
        f"It becomes Operational automatically when {base.code} is made Operational."
    )


def record_freed_booking_slots(equipment, slot_ids, *, user=None, source: str = "OTHER") -> None:
    """Freed slots that went back to Under Maintenance / Operator Absent join (or open) a disruption event."""
    from .disruption_service import DisruptionInput, record_unrecorded_slots
    from .models import DailySlot, SlotStatus

    if not slot_ids or equipment is None:
        return
    record_unrecorded_slots(
        equipment,
        DailySlot.objects.filter(pk__in=list(slot_ids), booking__isnull=True),
        DisruptionInput(user=user, source=source),
        old_status=SlotStatus.BOOKED,
    )


def _upcoming_slots(equipment):
    from .models import DailySlot

    return DailySlot.objects.filter(slot_master__equipment=equipment, date__gte=timezone.localdate())


DECISION_WINDOW_POLICY_SINCE = "2026-10-12"


def expired_disruption_bookings(now):
    """Disruption bookings still waiting for the user's choice (refund / reschedule) past the decision deadline.
    A refund ends the booking and a reschedule clears the disruption fields, so neither is selected."""
    from .models import Booking, BookingStatus

    return Booking.objects.filter(
        maintenance_disruption_flag=True,
        maintenance_decision_deadline_at__lte=now,
        status__in=[BookingStatus.BOOKED, BookingStatus.PENDING, BookingStatus.DISRUPTION_PENDING],
    )


def _booking_wallet_txns(booking, ref: str):
    from iic_booking.users.models.wallet import SubWalletTransaction

    return SubWalletTransaction.objects.filter(description__contains=ref)


def auto_cancel_dry_run(now=None) -> dict:
    """Read-only: what ``auto_cancel_expired_maintenance_bookings`` would do now (ids, counts and amounts only).

    For each booking: the refund amount, whether the refund sub-wallet is the one the booking was charged from,
    and anything that looks wrong (already credited, decided before the 5-day policy, no wallet, amount mismatch).
    """
    from datetime import date

    from iic_booking.communication.utils import booking_display_id_for_email
    from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction

    from .booking_paid_amount import booking_paid_charge

    now = now or timezone.now()
    since = date.fromisoformat(DECISION_WINDOW_POLICY_SINCE)
    rows, total = [], Decimal("0.00")
    for b in expired_disruption_bookings(now).select_related("user", "equipment").order_by("booking_id"):
        ref = booking_display_id_for_email(b) or str(b.booking_id)
        amount = booking_paid_charge(b)
        wallet = b.user.get_accessible_wallet() if b.user_id else None
        dept = getattr(b.equipment, "internal_department", None)
        target = None
        if wallet is not None:
            sub_q = SubWallet.objects.filter(wallet=wallet)
            target = (sub_q.filter(department=dept) if dept else sub_q.filter(department__code="GENERAL")).first()
        txns = _booking_wallet_txns(b, ref)
        debits = txns.filter(transaction_type=SubWalletTransaction.TransactionType.DEBIT)
        credits = txns.filter(transaction_type=SubWalletTransaction.TransactionType.CREDIT)
        debit_total = sum((t.amount for t in debits), Decimal("0.00"))
        credit_total = sum((t.amount for t in credits), Decimal("0.00"))
        debit_wallets = set(debits.values_list("sub_wallet_id", flat=True))
        marked_at = (
            b.maintenance_decision_deadline_at - timedelta(days=DISRUPTION_DECISION_DAYS)
            if b.maintenance_decision_deadline_at
            else None
        )
        flags = []
        if wallet is None:
            flags.append("no_wallet_would_cancel_without_refund")
        if amount > 0 and not debits.exists():
            flags.append("no_charge_debit_found")
        if debit_wallets and target is not None and debit_wallets != {target.pk}:
            flags.append("refund_wallet_differs_from_charged_wallet")
        if amount > 0 and debits.exists() and amount > debit_total - credit_total:
            flags.append("refund_exceeds_net_paid")
        if credit_total > 0:
            flags.append("already_has_credit")
        if marked_at and timezone.localtime(marked_at).date() < since:
            flags.append("disrupted_before_5_day_policy")
        if getattr(b.user, "is_test_account", False):
            flags.append("test_account")
        total += amount if wallet is not None else Decimal("0.00")
        rows.append(
            {
                "booking_id": b.booking_id,
                "equipment_code": getattr(b.equipment, "code", ""),
                "status": b.status,
                "disruption_kind": b.disruption_kind or "",
                "disrupted_on": timezone.localtime(marked_at).date().isoformat() if marked_at else None,
                "deadline": timezone.localtime(b.maintenance_decision_deadline_at).isoformat(timespec="minutes"),
                "refund_amount": str(amount),
                "charged": str(debit_total),
                "already_credited": str(credit_total),
                "flags": flags,
            }
        )
    return {
        "now": timezone.localtime(now).isoformat(timespec="minutes"),
        "count": len(rows),
        "total_refund": str(total),
        "flagged": sum(1 for r in rows if r["flags"]),
        "rows": rows,
    }


def auto_cancel_expired_maintenance_bookings() -> int:
    """Undecided disruption bookings past the decision deadline are marked Refunded with a full refund."""
    from .models import Booking, BookingStatus, BookingEventType, CancellationReason
    from .booking_audit import AUDIT_KEY, REASON_DISRUPTION_DEADLINE, disruption_kind_label, reason_fields
    from .booking_events import create_booking_event
    from iic_booking.users.repositories.wallet_repository import WalletRepository
    from iic_booking.equipment.api_views import (
        _reverse_reward_points_for_booking,
        _student_booking_description_suffix,
    )

    now = timezone.now()
    qs = expired_disruption_bookings(now).select_related("user", "equipment")
    cancelled = 0
    for booking in qs:
        try:
            with transaction.atomic():
                # Re-read under a row lock: a concurrent run, or the user's own refund / reschedule, wins.
                booking = (
                    expired_disruption_bookings(now)
                    .select_for_update(of=("self",))
                    .select_related("user", "equipment")
                    .filter(pk=booking.pk)
                    .first()
                )
                if booking is None:
                    continue
                released_slot_ids = list(booking.daily_slots.values_list("id", flat=True))
                previous_status = booking.status
                refund_amount = Decimal("0.00")
                slot_status = effective_slot_status_when_freeing_disruption_booking(booking)
                booking.daily_slots.update(
                    booking=None,
                    status=slot_status,
                )
                record_freed_booking_slots(booking.equipment, released_slot_ids, source="EQUIPMENT_STATUS")
                refund_target, _ = WalletRepository.get_booking_wallet_target(
                    booking.user, getattr(booking.equipment, "internal_department", None)
                )
                if refund_target:
                    from .booking_paid_amount import booking_paid_charge, clear_pending_charge_difference

                    refund_amount = booking_paid_charge(booking)
                    clear_pending_charge_difference(booking)
                    from iic_booking.communication.utils import booking_display_id_for_email

                    desc = (
                        f"Refund for Booking {booking_display_id_for_email(booking)} "
                        f"(no choice within {DISRUPTION_DECISION_DAYS} days of disruption) — "
                        f"{(getattr(booking.equipment, 'name', None) or getattr(booking.equipment, 'code', None) or '')}"
                    )
                    desc += _student_booking_description_suffix(refund_target, booking.user)
                    if refund_amount > 0:
                        refund_target.credit(
                            amount=refund_amount,
                            description=desc,
                            related_user=booking.user,
                        )
                    booking.status = BookingStatus.REFUNDED
                    new_status = BookingStatus.REFUNDED
                    event_type = BookingEventType.REFUNDED
                    _reverse_reward_points_for_booking(
                        booking, booking.user, "Reward points reversed for disruption auto-refund"
                    )
                else:
                    booking.status = BookingStatus.CANCELLED
                    new_status = BookingStatus.CANCELLED
                    event_type = BookingEventType.CANCELLED
                disruption_kind = booking.disruption_kind or ""
                clear_disruption_policy_fields(booking)
                booking.notes = (
                    (booking.notes or "")
                    + f"\n[Auto-refunded: no refund/reschedule choice within {DISRUPTION_DECISION_DAYS} days of the disruption.]"
                ).strip()
                booking.save()
                create_booking_event(
                    booking=booking,
                    event_type=event_type,
                    previous_status=previous_status,
                    new_status=new_status,
                    comment=(
                        f"Auto-refunded: no refund/reschedule choice within {DISRUPTION_DECISION_DAYS} days "
                        "of the disruption."
                    ),
                    created_by=None,
                    system_actor=True,
                    send_notification=False,
                    metadata={
                        AUDIT_KEY: reason_fields(
                            REASON_DISRUPTION_DEADLINE,
                            f"No refund/reschedule choice within {DISRUPTION_DECISION_DAYS} days of "
                            f"{disruption_kind_label(disruption_kind)}",
                            disruption_kind=disruption_kind,
                        ),
                        **({"refund_amount": str(refund_amount)} if refund_amount > 0 else {}),
                    },
                    cancellation={
                        "system": True,
                        "reason": CancellationReason.DISRUPTION_DEADLINE,
                        "refund_amount": refund_amount,
                        "released_slot_ids": released_slot_ids,
                    },
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

    from .disruption_service import record_unrecorded_upcoming_slots

    recorded = record_unrecorded_upcoming_slots(today)
    return {
        "equipments_checked": equipments.count(),
        "slots_marked_under_maintenance": slots_marked,
        "bookings_notified": bookings_notified,
        "disruption_events_opened": recorded["opened"],
        "disruption_events_extended": recorded["extended"],
    }
