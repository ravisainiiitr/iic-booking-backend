"""Celery tasks for facility user groups."""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import transaction

from iic_booking.equipment.peak_window import defer_during_peak

logger = logging.getLogger(__name__)


@shared_task(name="facility_groups.sync_booking_membership")
@defer_during_peak("facility_groups.sync_booking_membership")
def sync_booking_membership(booking_id: int):
    from .membership import auto_membership_enabled, sync_booking

    if not auto_membership_enabled():
        return None
    with transaction.atomic():
        return sync_booking(booking_id)


@shared_task(name="facility_groups.send_group_email")
@defer_during_peak("facility_groups.send_group_email")
def send_group_email(campaign_id: int):
    """Send one batch, then queue the next batch after a short pause until no recipient is pending."""
    from .group_email import batch_pause_seconds, dispatch, process_campaign

    result = process_campaign(campaign_id)
    if result.get("remaining"):
        dispatch(campaign_id, countdown=batch_pause_seconds())
    return result
