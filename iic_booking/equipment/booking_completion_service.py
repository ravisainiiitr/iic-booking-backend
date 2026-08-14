"""Shared booking completion transitions (manual + auto-complete)."""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.booking_events import create_booking_event
from iic_booking.equipment.booking_results_service import has_material_result_files
from iic_booking.equipment.models import Booking, BookingEventType, BookingStatus, BookingSampleTrace, SampleTraceStatus
from iic_booking.sync.models import BookingWorkspace

logger = logging.getLogger(__name__)

COMPLETABLE_STATUSES = (
    BookingStatus.PENDING,
    BookingStatus.BOOKED,
    BookingStatus.PROCESSING,
)


def booking_has_active_remote_analysis_session(booking: Booking) -> bool:
    """True when a live / in-progress Remote Analysis session must not be interrupted."""
    from iic_booking.remote_analysis.guacamole.authorization import OPEN_SESSION_STATUSES
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    return RemoteDesktopSession.objects.filter(
        booking_id=booking.pk,
        status__in=OPEN_SESSION_STATUSES,
    ).exists()


def auto_complete_candidate_queryset(now=None):
    """Restrict periodic scan to equipment with auto-complete enabled and ended slots."""
    now = now or timezone.now()
    return (
        Booking.objects.filter(
            equipment__auto_complete_booking=True,
            status__in=COMPLETABLE_STATUSES,
            daily_slots__end_datetime__lte=now,
        )
        .select_related("equipment", "user")
        .distinct()
    )


def try_auto_complete_booking(booking: Booking, *, send_email: bool = True) -> tuple[bool, str]:
    """
    Complete one booking when auto-complete rules are satisfied.

    Returns (completed, skip_reason). skip_reason is empty on success.
    Idempotent: already-completed bookings return (False, "already_completed").
    """
    if not getattr(getattr(booking, "equipment", None), "auto_complete_booking", False):
        return False, "auto_complete_disabled"

    if not BookingWorkspace.objects.filter(booking_id=booking.pk).exists():
        return False, "WORKSPACE_NOT_FOUND"

    if not has_material_result_files(booking):
        return False, "NO_RESULT_DATA"

    if booking_has_active_remote_analysis_session(booking):
        return False, "ACTIVE_RAA_SESSION"

    completed = False
    booking_id = booking.pk
    with transaction.atomic():
        locked = (
            Booking.objects.select_for_update()
            .select_related("equipment", "user")
            .get(pk=booking.pk)
        )
        if locked.status == BookingStatus.COMPLETED:
            return False, "already_completed"
        if locked.status not in COMPLETABLE_STATUSES:
            return False, f"invalid_status:{locked.status}"
        if booking_has_active_remote_analysis_session(locked):
            return False, "ACTIVE_RAA_SESSION"
        if not has_material_result_files(locked):
            return False, "NO_RESULT_DATA"

        previous_status = locked.status
        locked.status = BookingStatus.COMPLETED
        locked.completed_at = timezone.now()
        locked.save(update_fields=["status", "completed_at", "updated_at"])
        create_booking_event(
            booking=locked,
            event_type=BookingEventType.COMPLETED,
            previous_status=previous_status,
            new_status=BookingStatus.COMPLETED,
            comment=(
                "Booking automatically completed after scheduled end time because "
                "result data was detected in the Active workspace."
            ),
            metadata={
                "completion_method": "AUTO_COMPLETE",
                "completion_source": "AUTO_COMPLETE",
                "reason": "result data detected after booking end time",
                "equipment_id": locked.equipment_id,
            },
            created_by=None,
            system_actor=True,
            send_notification=False,
        )
        booking_id = locked.booking_id
        completed = True

    if completed:
        try:
            has_completed_trace = BookingSampleTrace.objects.filter(
                booking_id=booking_id, status=SampleTraceStatus.COMPLETED
            ).exists()
            if not has_completed_trace:
                BookingSampleTrace.objects.create(
                    booking_id=booking_id,
                    status=SampleTraceStatus.COMPLETED,
                    created_by=None,
                    reason="Recorded automatically when booking was auto-completed.",
                )
        except Exception:
            logger.exception("Failed to write COMPLETED sample trace on auto-complete booking %s", booking_id)

        if send_email:
            try:
                from iic_booking.equipment.api_views import _send_completion_email_with_attachments

                refreshed = Booking.objects.select_related("equipment", "user").get(booking_id=booking_id)
                # Never attach result files to email — portal Booking Details remains the download path.
                _send_completion_email_with_attachments(refreshed, [])
            except Exception:
                logger.exception("auto-complete completion email failed booking_id=%s", booking_id)

    return completed, ""
