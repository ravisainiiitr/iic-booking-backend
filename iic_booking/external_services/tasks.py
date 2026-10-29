"""Scheduled work for external-user services.

``prepare_window`` (every 15 minutes) records each week the central external window has opened and prepares it
once: missing daily slots are generated (as the Wednesday next-week task does) and the weekly external quota
snapshot is taken after the slots exist. Booking enforcement never depends on this task: the window itself is
computed from the configuration and the clock, so a missed or late run only delays the preparation, which the
next run completes. Re-runs and concurrent runs are no-ops (unique week rows, per-step timestamps, rows claimed
with ``skip_locked``).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Generate the next week's slots this long before it opens, so the first page loads at the opening are reads.
PREPARE_AHEAD = timedelta(hours=1)


def _operational_equipment():
    from iic_booking.equipment.models import Equipment, EquipmentStatus, SlotMaster

    ids = (
        SlotMaster.objects.filter(is_active=True, equipment__status=EquipmentStatus.ACTIVE)
        .values_list("equipment_id", flat=True)
        .distinct()
    )
    return Equipment.objects.filter(equipment_id__in=list(ids))


def _generate_slots(week_start) -> int:
    from iic_booking.equipment.tasks import _generate_week_slots_for_operational_equipment

    return _generate_week_slots_for_operational_equipment(
        [week_start], allow_holiday=True, log_label="external_window.prepare"
    )


def _ensure_snapshots(week_start) -> int:
    from iic_booking.equipment.external_slot_quota import ExternalSlotQuotaService
    from iic_booking.equipment.models import ExternalWeeklySlotQuotaSnapshot

    created = 0
    for equipment in _operational_equipment():
        existed = ExternalWeeklySlotQuotaSnapshot.objects.filter(equipment=equipment, week_start=week_start).exists()
        ExternalSlotQuotaService.ensure_snapshot(equipment, week_start)
        created += 0 if existed else 1
    return created


def prepare_open_weeks(at=None) -> dict:
    from .models import ExternalBookingWindowOpening
    from .window import external_window

    at = at or timezone.now()
    window = external_window(at)
    if not window.enabled:
        return {"skipped": "disabled"}
    result = {"recorded": 0, "slots_prepared": 0, "quota_prepared": 0, "ahead": 0}
    if window.is_open and window.first_date and window.last_date:
        today = timezone.localtime(at).date()
        monday = window.first_date - timedelta(days=window.first_date.weekday())
        while monday <= window.last_date:
            if monday + timedelta(days=6) >= today:
                _row, created = ExternalBookingWindowOpening.objects.get_or_create(week_start=monday)
                result["recorded"] += int(created)
            monday += timedelta(days=7)
        pending = list(
            ExternalBookingWindowOpening.objects.filter(
                week_start__gte=today - timedelta(days=6), week_start__lte=window.last_date
            ).filter(quota_prepared_at__isnull=True).values_list("pk", flat=True)
        )
        for pk in pending:
            with transaction.atomic():
                row = (
                    ExternalBookingWindowOpening.objects.select_for_update(skip_locked=True)
                    .filter(pk=pk, quota_prepared_at__isnull=True)
                    .first()
                )
                if row is None:  # another worker has it, or it is done
                    continue
                if row.slots_prepared_at is None:
                    row.slots_created = _generate_slots(row.week_start)
                    row.slots_prepared_at = timezone.now()
                    result["slots_prepared"] += 1
                row.snapshots_created = _ensure_snapshots(row.week_start)
                row.quota_prepared_at = timezone.now()
                row.save(update_fields=["slots_created", "slots_prepared_at", "snapshots_created", "quota_prepared_at"])
                result["quota_prepared"] += 1
    nxt = window.next_week_start
    opening_at = window.next_expansion_at if window.is_open else window.opens_at
    if nxt and opening_at and timedelta(0) < opening_at - at <= PREPARE_AHEAD:
        result["ahead"] = _generate_slots(nxt)  # slots only; the quota snapshot is taken once the week is open
    logger.info("external_window.prepare: %s", result)
    return result


@shared_task(name="external_services.prepare_window")
def prepare_window() -> dict:
    return prepare_open_weeks()


@shared_task(name="external_services.expire_special_allocations")
def expire_special_allocations() -> dict:
    """Every 5 minutes: cancel special allocations not paid by their deadline (each one re-checked under lock), and
    close booking checkouts whose online-payment window passed (they hold no slot; this only tidies their state)."""
    from .checkout import expire_overdue as expire_checkouts
    from .special_payment import expire_overdue

    result = expire_overdue()
    if result:
        logger.info("external special expiry: %s", result)
    closed = expire_checkouts()
    if closed:
        logger.info("external booking checkouts expired: %s", closed)
    return result


@shared_task(name="external_services.queue_completed_invoice_requests")
def queue_completed_invoice_requests() -> dict:
    """Every 30 minutes: queue SRIC invoice requests for completed, paid external bookings (idempotent)."""
    from .invoices import queue_completed

    return queue_completed()


@shared_task(name="external_services.sric_invoice_batch")
def sric_invoice_batch() -> dict:
    """09:30 and every 30 minutes until 17:00 on weekdays; does nothing on holidays or while dispatch is off."""
    from .sric_dispatch import run_batch

    return run_batch()


@shared_task(name="external_services.notifications_sweep")
def notifications_sweep() -> dict:
    """Every 10 minutes: retry failed notifications; mark ones stuck mid-send as uncertain (never resent)."""
    from .notifications import sweep

    return sweep()
