"""Simplified workflow for fabrication bookings (3D printing and 2D laser cutting).

- Lab staff can reject a booked job as not feasible, with a reason. The booking stays Booked and keeps
  its slot; the user may upload new files until ``fabrication_replace_deadline``.
- Replacing the files clears the rejection (history stays in the booking events).
- ``expire_fabrication_rejections`` cancels rejected bookings past the deadline with a full refund.
- Completion sends a "ready for pickup" email instead of the sample-oriented completion email.
"""

import logging
from datetime import timedelta
from html import escape

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iic_booking.communication.email_branding import absolute_http_url, format_email_datetime, format_inr, user_display_name
from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url

from .fabrication import (
    active_print_analyses_for_booking,
    fabrication_parts_summary,
    is_fabrication_equipment,
)
from .models import Booking, BookingEventType, BookingStatus, EquipmentOperator, EquipmentProfileType

logger = logging.getLogger(__name__)

REJECTION_REASON_MIN_LENGTH = 10
REJECTION_REASON_MAX_LENGTH = 2000
REPLACE_WINDOW_DEFAULT_HOURS = 24
REPLACE_WINDOW_MIN_HOURS = 1
REPLACE_WINDOW_MAX_HOURS = 168
REJECTION_METADATA_KEY = "fabrication_rejection"

REJECTED_EMAIL = "fabrication_booking_rejected_email"
REPLACED_EMAIL = "fabrication_files_replaced_email"
PICKUP_EMAIL = "fabrication_ready_for_pickup_email"

PICKUP_INSTRUCTIONS = (
    "Please collect your parts from the laboratory during working hours and quote your booking ID. "
    "If you need a different time, contact the Lab Operator below."
)

SAMPLE_STATUS_REFUSED_MESSAGE = (
    "3D printing and laser cutting bookings do not use the sample lifecycle. "
    "Use Mark complete, or Reject (not feasible) if the job cannot be done."
)


class FabricationWorkflowError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def replace_window_hours(equipment) -> int:
    try:
        hours = int(getattr(equipment, "fabrication_replace_window_hours", None) or REPLACE_WINDOW_DEFAULT_HOURS)
    except (TypeError, ValueError):
        hours = REPLACE_WINDOW_DEFAULT_HOURS
    return max(REPLACE_WINDOW_MIN_HOURS, min(REPLACE_WINDOW_MAX_HOURS, hours))


def clean_replace_window_hours(raw):
    """Returns (hours, error) for a value entered on the settings pages."""
    try:
        hours = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, "Enter the replace window as a whole number of hours."
    if hours < REPLACE_WINDOW_MIN_HOURS or hours > REPLACE_WINDOW_MAX_HOURS:
        return None, (
            f"The replace window must be between {REPLACE_WINDOW_MIN_HOURS} and {REPLACE_WINDOW_MAX_HOURS} hours."
        )
    return hours, None


def is_rejection_active(booking) -> bool:
    return bool(
        booking is not None
        and booking.status == BookingStatus.BOOKED
        and getattr(booking, "fabrication_rejected_at", None) is not None
    )


def user_is_fabrication_lab_staff(user, booking) -> bool:
    """Admins, the equipment's (temporary) OIC and its Lab Operators."""
    from .api_views import _is_admin_user, _user_can_act_as_oic_for_equipment

    if not user or not getattr(user, "is_authenticated", False):
        return False
    if _is_admin_user(user) or _user_can_act_as_oic_for_equipment(user, booking.equipment):
        return True
    return EquipmentOperator.objects.filter(equipment_id=booking.equipment_id, operator=user).exists()


user_can_reject_fabrication = user_is_fabrication_lab_staff


def fabrication_reject_block_reason(booking) -> str | None:
    from .input_edit_payment_window import has_payment_window

    if not is_fabrication_equipment(getattr(booking, "equipment", None)):
        return "Only 3D printing and laser cutting bookings can be rejected as not feasible."
    if booking.status != BookingStatus.BOOKED:
        return "Only Booked bookings can be rejected."
    if booking.fabrication_rejected_at is not None:
        return "This booking is already rejected and is waiting for new files."
    if booking.source_booking_id is not None:
        return "Repeat bookings cannot be rejected. Cancel the booking instead."
    if has_payment_window(booking):
        return "The user has an unpaid file change on this booking. Please try again in a few minutes."
    return None


# --------------------------------------------------------------------------- payload / snapshot


def _person_name(person) -> str:
    if person is None:
        return ""
    return user_display_name(person, fallback="") or (getattr(person, "email", "") or "")


def fabrication_workflow_payload(booking, user=None, now=None) -> dict | None:
    """Rejection state for the booking detail; None for other equipment."""
    equipment = getattr(booking, "equipment", None)
    if not is_fabrication_equipment(equipment):
        return None
    now = now or timezone.now()
    active = is_rejection_active(booking)
    deadline = booking.fabrication_replace_deadline if active else None
    can_reject = bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and fabrication_reject_block_reason(booking) is None
        and user_is_fabrication_lab_staff(user, booking)
    )
    return {
        "rejected": active,
        "rejected_at": booking.fabrication_rejected_at.isoformat() if active else None,
        "rejected_by_name": _person_name(booking.fabrication_rejected_by) if active else "",
        "reason": booking.fabrication_rejection_reason if active else "",
        "replace_deadline": deadline.isoformat() if deadline else None,
        "replace_deadline_display": format_email_datetime(deadline) if deadline else "",
        "replace_deadline_passed": bool(deadline and now >= deadline),
        "replace_window_hours": replace_window_hours(equipment),
        "can_reject": can_reject,
        "reason_min_length": REJECTION_REASON_MIN_LENGTH,
    }


def rejection_snapshot(booking) -> dict | None:
    if booking.fabrication_rejected_at is None:
        return None
    return {
        "rejected_at": booking.fabrication_rejected_at.isoformat(),
        "rejected_by_id": booking.fabrication_rejected_by_id,
        "reason": booking.fabrication_rejection_reason or "",
        "replace_deadline": (
            booking.fabrication_replace_deadline.isoformat() if booking.fabrication_replace_deadline else None
        ),
    }


def restore_rejection(booking, snapshot) -> bool:
    """Put back a rejection captured by ``rejection_snapshot`` (used when an unpaid file change is undone)."""
    from django.utils.dateparse import parse_datetime

    if not isinstance(snapshot, dict) or not snapshot.get("rejected_at"):
        return False
    booking.fabrication_rejected_at = parse_datetime(snapshot["rejected_at"])
    booking.fabrication_rejected_by_id = snapshot.get("rejected_by_id")
    booking.fabrication_rejection_reason = snapshot.get("reason") or ""
    deadline = snapshot.get("replace_deadline")
    booking.fabrication_replace_deadline = parse_datetime(deadline) if deadline else None
    booking.save(update_fields=_REJECTION_FIELDS + ["updated_at"])
    return True


_REJECTION_FIELDS = [
    "fabrication_rejected_at",
    "fabrication_rejected_by",
    "fabrication_rejection_reason",
    "fabrication_replace_deadline",
]


def clear_rejection(booking) -> dict | None:
    """Clear an active rejection; returns what was cleared."""
    previous = rejection_snapshot(booking)
    if previous is None:
        return None
    booking.fabrication_rejected_at = None
    booking.fabrication_rejected_by = None
    booking.fabrication_rejection_reason = ""
    booking.fabrication_replace_deadline = None
    booking.save(update_fields=_REJECTION_FIELDS + ["updated_at"])
    return previous


# --------------------------------------------------------------------------- reject


def reject_fabrication_booking(booking_id, actor, reason, *, now=None) -> Booking:
    """Mark a booked fabrication job as not feasible and email the user. Raises FabricationWorkflowError."""
    from .booking_events import create_booking_event

    reason = " ".join(str(reason or "").split())
    if len(reason) < REJECTION_REASON_MIN_LENGTH:
        raise FabricationWorkflowError(
            f"Please explain why the job is not feasible (at least {REJECTION_REASON_MIN_LENGTH} characters)."
        )
    reason = reason[:REJECTION_REASON_MAX_LENGTH]
    now = now or timezone.now()
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("equipment", "user")
            .filter(booking_id=booking_id)
            .first()
        )
        if booking is None:
            raise FabricationWorkflowError("Booking not found.", status_code=404)
        if not user_is_fabrication_lab_staff(actor, booking):
            raise FabricationWorkflowError(
                "Only the Lab Operators, the Officer In Charge of this equipment or an administrator can reject it.",
                status_code=403,
            )
        block = fabrication_reject_block_reason(booking)
        if block:
            raise FabricationWorkflowError(block)
        hours = replace_window_hours(booking.equipment)
        deadline = now + timedelta(hours=hours)
        booking.fabrication_rejected_at = now
        booking.fabrication_rejected_by = actor
        booking.fabrication_rejection_reason = reason
        booking.fabrication_replace_deadline = deadline
        booking.save(update_fields=_REJECTION_FIELDS + ["updated_at"])
        create_booking_event(
            booking=booking,
            event_type=BookingEventType.COMMENT,
            created_by=actor,
            comment=(
                f"Rejected as not feasible: {reason} "
                f"The user can upload new files until {format_email_datetime(deadline)}."
            ),
            metadata={
                REJECTION_METADATA_KEY: {
                    "action": "rejected",
                    "reason": reason,
                    "replace_deadline": deadline.isoformat(),
                    "window_hours": hours,
                }
            },
            send_notification=False,
        )
        booking_pk = booking.pk
        transaction.on_commit(lambda: _safely(send_rejection_email, booking_pk))
    return booking


# --------------------------------------------------------------------------- replace


def after_files_replaced(booking, actor) -> bool:
    """Clear an active rejection after a successful file change; True when one was cleared."""
    from .booking_events import create_booking_event

    if not is_rejection_active(booking):
        return False
    previous = clear_rejection(booking)
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        created_by=actor,
        comment="New files uploaded after the rejection. The booking is active again.",
        metadata={REJECTION_METADATA_KEY: {"action": "replaced", "previous": previous}},
        send_notification=False,
    )
    booking_pk = booking.pk
    transaction.on_commit(lambda: _safely(send_files_replaced_email, booking_pk))
    return True


# --------------------------------------------------------------------------- expiry


def _window_hours_used(booking) -> int:
    if booking.fabrication_rejected_at and booking.fabrication_replace_deadline:
        seconds = (booking.fabrication_replace_deadline - booking.fabrication_rejected_at).total_seconds()
        return max(1, round(seconds / 3600))
    return replace_window_hours(booking.equipment)


def expiry_cancel_reason(booking) -> str:
    return f"Files not replaced within {_window_hours_used(booking)} hours after rejection"


def expire_fabrication_rejections(now=None, limit: int = 200) -> int:
    """Cancel (full refund) rejected fabrication bookings whose replace deadline has passed. Idempotent."""
    now = now or timezone.now()
    ids = list(
        Booking.objects.filter(
            status=BookingStatus.BOOKED,
            fabrication_rejected_at__isnull=False,
            fabrication_replace_deadline__lte=now,
        )
        .order_by("fabrication_replace_deadline")
        .values_list("booking_id", flat=True)[:limit]
    )
    cancelled = 0
    for booking_id in ids:
        try:
            if _expire_one(booking_id, now):
                cancelled += 1
        except Exception:
            logger.exception("Could not cancel expired fabrication rejection booking_id=%s", booking_id)
    return cancelled


def _expire_one(booking_id, now) -> bool:
    from .api_views import _reverse_reward_points_for_booking, _student_booking_description_suffix
    from .booking_cancellation import perform_booking_cancellation
    from .input_edit_payment_window import expire_unpaid_input_edit

    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("equipment", "user")
            .filter(booking_id=booking_id)
            .first()
        )
        if booking is None or not is_rejection_active(booking):
            return False
        expire_unpaid_input_edit(booking)
        booking.refresh_from_db()
        if not is_rejection_active(booking) or booking.fabrication_replace_deadline is None:
            return False
        if booking.fabrication_replace_deadline > now:
            return False
        reason = expiry_cancel_reason(booking)
        result = perform_booking_cancellation(
            booking,
            slot_ids=list(booking.daily_slots.values_list("id", flat=True)),
            should_refund=True,
            cancel_notes=reason,
            actor=None,
            allow_started_slots=True,
            reverse_reward_points_fn=_reverse_reward_points_for_booking,
            student_booking_description_suffix_fn=_student_booking_description_suffix,
            cancelled_by_label="system",
        )
        refund_amount = result.get("refund_amount")
        transaction.on_commit(
            lambda: _safely(send_expiry_lab_email, booking_id, reason, str(refund_amount or "0"))
        )
    return True


# --------------------------------------------------------------------------- emails


def _safely(fn, *args):
    try:
        fn(*args)
    except Exception:
        logger.exception("Fabrication workflow email %s failed for %s", getattr(fn, "__name__", fn), args)


def _ensure_template(code: str) -> None:
    from .booking_lab_messages import ensure_email_template

    ensure_email_template(code)


def _booking_link(booking, *, staff: bool = False) -> str:
    if staff:
        path = f"/booking-management?expand={booking.booking_id}"
    else:
        path = f"/my-bookings?booking={booking_display_id_for_email(booking)}"
    return absolute_http_url(get_frontend_absolute_url(path))


def _print_material_label(booking) -> str:
    for analysis in active_print_analyses_for_booking(booking):
        material = getattr(analysis, "material", None)
        if material is not None:
            return material.name or material.code or ""
        if analysis.material_code_snapshot:
            return analysis.material_code_snapshot
    return ""


def user_part_lines(booking) -> list[str]:
    """"Name × qty — material" for each part (no internal estimates)."""
    parts = fabrication_parts_summary(booking)
    if booking.own_material:
        print_material = "your own material"
    elif booking.equipment.profile_type == EquipmentProfileType.PRINT_3D:
        print_material = _print_material_label(booking)
    else:
        print_material = ""
    lines = []
    for part in parts:
        if part.get("kind") == "laser":
            material = "your own material" if booking.own_material else (
                part.get("material_name") or part.get("material_code") or ""
            )
        else:
            material = print_material
        line = f"{part.get('name') or part.get('filename') or 'Part'} × {part.get('quantity') or 1}"
        lines.append(f"{line} — {material}" if material else line)
    return lines


def _parts_context(booking) -> dict:
    lines = user_part_lines(booking)
    return {
        "parts_display": "<br/>".join(escape(line) for line in lines),
        "parts_text": "\n".join(f"  {line}" for line in lines),
    }


def _base_context(booking) -> dict:
    from .booking_events import apply_booking_party_to_context

    equipment = booking.equipment
    ctx = {
        "user_name": user_display_name(booking.user),
        "user_email": booking.user.email or "",
        "booking_id": booking_display_id_for_email(booking),
        "virtual_booking_id": booking_display_id_for_email(booking),
        "equipment_name": equipment.name,
        "equipment_code": equipment.code or "",
        "link": _booking_link(booking),
        **_parts_context(booking),
    }
    apply_booking_party_to_context(ctx, booking)
    return ctx


def _send_user_email(booking, template_code: str, context: dict, event: str) -> None:
    from iic_booking.communication.service import CommunicationService

    if not (booking.user and (booking.user.email or "").strip()):
        return
    _ensure_template(template_code)
    CommunicationService.send_email(
        recipient=booking.user,
        template=template_code,
        template_context=context,
        metadata={
            "booking_id": context.get("booking_id"),
            "real_booking_id": booking.booking_id,
            "event": event,
            "link": context.get("link"),
        },
    )


def send_rejection_email(booking_pk) -> None:
    booking = Booking.objects.select_related("equipment", "user", "fabrication_rejected_by", "charge_profile").get(
        pk=booking_pk
    )
    if not is_rejection_active(booking):
        return
    deadline = booking.fabrication_replace_deadline
    ctx = _base_context(booking)
    ctx.update(
        {
            "rejection_reason": booking.fabrication_rejection_reason,
            "replace_deadline_display": format_email_datetime(deadline),
            "replace_window_hours": str(_window_hours_used(booking)),
            "rejected_by_display": _person_name(booking.fabrication_rejected_by),
        }
    )
    _send_user_email(booking, REJECTED_EMAIL, ctx, "fabrication.rejected")
    _notify_in_app(
        [booking.user],
        title="Action needed: replace your files",
        message=(
            f"{ctx['booking_id']} — {booking.equipment.name}: the lab cannot make the parts as uploaded. "
            f"Upload new files by {ctx['replace_deadline_display']}."
        ),
        link=f"/my-bookings?booking={ctx['booking_id']}",
        booking=booking,
        event="fabrication.rejected",
        warning=True,
    )


def send_files_replaced_email(booking_pk) -> None:
    from .print_3d_notifications import REASON_FILES_REPLACED_AFTER_REJECTION, dispatch_fabrication_file_email
    from .reports import get_equipment_staff_notify_users

    booking = Booking.objects.select_related("equipment", "user", "charge_profile").get(pk=booking_pk)
    dispatch_fabrication_file_email(booking, reason=REASON_FILES_REPLACED_AFTER_REJECTION)
    ctx = _base_context(booking)
    ctx["total_charge"] = format_inr(booking.total_charge)
    _send_user_email(booking, REPLACED_EMAIL, ctx, "fabrication.files_replaced")

    _notify_in_app(
        get_equipment_staff_notify_users(booking.equipment),
        title="New files uploaded after rejection",
        message=f"{ctx['booking_id']} — {booking.equipment.name}: the user uploaded new files. Please check them.",
        link=f"/booking-management?expand={booking.booking_id}",
        booking=booking,
        event="fabrication.files_replaced",
    )


def send_expiry_lab_email(booking_id, reason: str, refund_amount: str) -> None:
    """Tell the fabrication notification list (staff users get the standard refund email)."""
    from django.core.mail import EmailMessage

    from .print_3d_notifications import notification_recipients
    from .reports import get_equipment_staff_notify_users

    booking = Booking.objects.select_related("equipment", "user", "charge_profile").get(booking_id=booking_id)
    staff_emails = {
        (getattr(u, "email", "") or "").strip().lower() for u in get_equipment_staff_notify_users(booking.equipment)
    }
    recipients = [e for e in notification_recipients(booking.equipment) if e.lower() not in staff_emails]
    if not recipients:
        return
    from .fbr_email import fbr_text_line

    display_id = booking_display_id_for_email(booking)
    lines = [
        f"Booking {display_id} was cancelled automatically: {reason}.",
        f"A full refund of {format_inr(refund_amount) or '₹0.00'} was issued and the slot was released.",
        "",
        f"Booking ID: {display_id}",
    ]
    fbr_line = fbr_text_line(booking)
    if fbr_line:
        lines.append(fbr_line)
    lines += [
        f"Equipment: {booking.equipment.name} ({booking.equipment.code})",
        f"Booked by: {_person_name(booking.user) or '—'} ({booking.user.email or '—'})",
        "",
        "Institute Instrumentation Centre, IIT Roorkee.",
    ]
    EmailMessage(
        subject=f"Booking cancelled (files not replaced) — {display_id} — {booking.equipment.code or booking.equipment.name}",
        body="\n".join(lines),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    ).send(fail_silently=False)


def send_pickup_email(booking) -> None:
    """Completion email for fabrication bookings: parts ready for pickup."""
    from .booking_events import apply_equipment_completion_email_extra_to_context, apply_lab_visit_details_to_context

    ctx = _base_context(booking)
    ctx["total_charge"] = format_inr(booking.total_charge)
    ctx["pickup_instructions"] = PICKUP_INSTRUCTIONS
    apply_lab_visit_details_to_context(ctx, booking.equipment)
    apply_equipment_completion_email_extra_to_context(ctx, booking.equipment)
    _send_user_email(booking, PICKUP_EMAIL, ctx, "fabrication.ready_for_pickup")


def _notify_in_app(users, *, title, message, link, booking, event, warning=False) -> None:
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [u for u in users if u is not None],
            title=title,
            message=message,
            link=link,
            notification_type="warning" if warning else "info",
            event=event,
            extra={"real_booking_id": booking.booking_id},
        )
    except Exception:
        logger.exception("In-app notification %s failed for booking_id=%s", event, booking.booking_id)
