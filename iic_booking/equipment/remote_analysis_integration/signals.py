"""Signals bridging booking lifecycle to Remote Analysis (without changing booking status)."""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender="equipment.Booking")
def booking_completed_evaluate_analysis(sender, instance, created, **kwargs):
    """When booking reaches COMPLETED, evaluate eligibility and create reservation if enabled."""
    if created:
        return
    update_fields = kwargs.get("update_fields")
    # Always check status; skip if clearly not completed
    from iic_booking.equipment.models import BookingStatus

    if instance.status != BookingStatus.COMPLETED:
        return
    # Avoid recursion when we only update analysis_* fields
    if update_fields is not None:
        analysis_only = set(update_fields) <= {
            "analysis_available",
            "analysis_available_from",
            "analysis_expiry",
            "analysis_session_count",
            "analysis_last_session",
            "analysis_reservation",
            "analysis_workspace",
            "updated_at",
        }
        if analysis_only:
            return
        if "status" not in update_fields and "completed_at" not in update_fields:
            # status may have been saved without update_fields listing — still allow when completed_at set
            if not instance.completed_at:
                return
    try:
        from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService

        BookingRemoteAnalysisService().on_booking_completed(instance)
    except Exception:
        logger.exception("booking_completed_evaluate_analysis failed booking=%s", instance.booking_id)


@receiver(post_save, sender="equipment.Booking")
def booking_terminal_cancel_analysis_holds(sender, instance, created, **kwargs):
    """Refunded / cancelled bookings drop idle Remote Analysis holds once the booking save commits."""
    if created:
        return
    from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService

    if instance.status not in BookingAnalysisEligibilityService.TERMINAL_BLOCK:
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "status" not in update_fields:
        return
    booking_pk = instance.pk
    # After commit so a failure here can never roll back the refund / cancellation itself.
    transaction.on_commit(lambda: _cancel_idle_analysis_holds(booking_pk))


def _cancel_idle_analysis_holds(booking_pk) -> None:
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
    from iic_booking.remote_analysis.services.scheduler import IDLE_HOLD_STATUSES, SchedulerService

    scheduler = SchedulerService()
    for reservation in AnalysisReservation.objects.filter(
        booking_id=booking_pk, status__in=IDLE_HOLD_STATUSES
    ).select_related("booking", "booking__equipment", "workstation"):
        try:
            with transaction.atomic():
                scheduler.cancel_if_booking_ineligible(reservation)
        except Exception:
            logger.exception(
                "booking_terminal_cancel_analysis_holds failed booking=%s reservation=%s",
                booking_pk,
                reservation.pk,
            )


@receiver(post_save, sender="remote_analysis.AnalysisReservation")
def reservation_sync_booking_fields(sender, instance, **kwargs):
    if not instance.booking_id:
        return
    try:
        from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService

        BookingRemoteAnalysisService().sync_from_reservation(instance)
    except Exception:
        logger.exception("reservation_sync_booking_fields failed reservation=%s", instance.id)


@receiver(post_save, sender="remote_analysis.AnalysisWorkspace")
def workspace_archived_update_booking(sender, instance, **kwargs):
    if not instance.booking_id:
        return
    from iic_booking.remote_analysis.constants import WorkspaceStatus

    if instance.status != WorkspaceStatus.ARCHIVED and not instance.archived_at:
        return
    try:
        booking = instance.booking
        if booking and booking.analysis_workspace_id == instance.id:
            booking.analysis_available = False
            booking.save(update_fields=["analysis_available", "updated_at"])
    except Exception:
        logger.exception("workspace_archived_update_booking failed")
