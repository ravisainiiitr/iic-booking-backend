"""Add booking users to their automatic groups after a booking is created or changes status.

Runs after the booking's transaction commits and never raises, so booking keeps working before the
``facility_groups`` migration is applied (the sync just logs and the backfill fills the gap later).
During the slot-opening peak window the sync is handed to Celery, which defers it until the window ends.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_init, post_save
from django.dispatch import receiver

from iic_booking.equipment.models import Booking

logger = logging.getLogger(__name__)

_STATUS_ATTR = "_facility_groups_loaded_status"


@receiver(post_init, sender=Booking)
def _remember_status(sender, instance, **kwargs):
    instance.__dict__[_STATUS_ATTR] = instance.__dict__.get("status")


def run_sync(booking_id: int) -> None:
    from .membership import auto_membership_enabled, sync_booking

    if not auto_membership_enabled():
        return
    try:
        from iic_booking.equipment.peak_window import seconds_until_peak_end_for_deferral

        if seconds_until_peak_end_for_deferral():
            from .tasks import sync_booking_membership

            sync_booking_membership.delay(booking_id)
            return
    except Exception:
        logger.debug("facility groups: peak deferral unavailable, syncing now", exc_info=True)
    try:
        with transaction.atomic():
            sync_booking(booking_id)
    except Exception:
        logger.warning("facility groups: could not record booking %s in its groups", booking_id, exc_info=True)


@receiver(post_save, sender=Booking)
def _booking_saved(sender, instance, created, update_fields=None, raw=False, **kwargs):
    if raw:
        return
    status = instance.__dict__.get("status")
    previous = instance.__dict__.get(_STATUS_ATTR)
    instance.__dict__[_STATUS_ATTR] = status
    if not created and status == previous:
        return
    if update_fields is not None and not created and "status" not in update_fields:
        return
    booking_id = instance.pk
    transaction.on_commit(lambda: run_sync(booking_id))
