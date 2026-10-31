"""Retired courier-tracking tasks.

Automatic tracking was removed (31 Oct 2026 committee). Migration 0003 deletes the periodic schedules; until it runs,
and for any message already queued, these names stay registered and do nothing, so the worker never fails on an
unknown task.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

RETIRED = "retired: automatic courier tracking was removed"


def _retired(name: str) -> str:
    logger.info("courier_tracking.%s skipped (%s)", name, RETIRED)
    return RETIRED


@shared_task(name="courier_tracking.lookup")
def lookup_task(*args, **kwargs) -> str:
    return _retired("lookup")


@shared_task(name="courier_tracking.scheduled_checks")
def scheduled_checks_task(*args, **kwargs) -> str:
    return _retired("scheduled_checks")


@shared_task(name="courier_tracking.send_notifications")
def send_notifications_task(*args, **kwargs) -> str:
    return _retired("send_notifications")


@shared_task(name="courier_tracking.sweep")
def sweep_task(*args, **kwargs) -> str:
    return _retired("sweep")


@shared_task(name="courier_tracking.process_webhook")
def process_webhook_task(*args, **kwargs) -> str:
    return _retired("process_webhook")


@shared_task(name="courier_tracking.reminders")
def reminders_task(*args, **kwargs) -> str:
    return _retired("reminders")


@shared_task(name="courier_tracking.refresh_catalog")
def refresh_catalog_task(*args, **kwargs) -> str:
    return _retired("refresh_catalog")
