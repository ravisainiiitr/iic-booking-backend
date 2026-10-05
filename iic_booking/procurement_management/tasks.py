"""Celery tasks for Procurement & Assets. Not scheduled by default — add a beat entry when the module goes live."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="procurement_management.amc_reminders")
def amc_reminders_task() -> dict:
    from iic_booking.procurement_management.amc import send_reminders

    result = send_reminders()
    logger.info("procurement_management.amc_reminders: %s", result)
    return result
