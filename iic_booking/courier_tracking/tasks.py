"""Celery tasks. Every queued action is also recorded in the database, so these tasks can be lost or repeated safely:
the periodic sweeps pick up whatever a lost message left behind."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="courier_tracking.lookup")
def lookup_task(arrival_id: int, trigger: str) -> str:
    from .services import lookup

    result = lookup(arrival_id, trigger)
    return result.outcome if result.performed else f"refused:{result.refused}"


@shared_task(name="courier_tracking.scheduled_checks")
def scheduled_checks_task() -> dict:
    from .services import run_scheduled_checks

    return run_scheduled_checks()


@shared_task(name="courier_tracking.send_notifications")
def send_notifications_task(ids: list[int]) -> dict:
    from .notifications import send_one

    counts: dict[str, int] = {}
    for pk in ids or []:
        result = send_one(pk)
        counts[result] = counts.get(result, 0) + 1
    return counts


@shared_task(name="courier_tracking.sweep")
def sweep_task() -> dict:
    """Retry notifications and webhook events left pending or failed."""
    from . import notifications, webhooks

    return {"notifications": notifications.sweep(), "webhooks": webhooks.sweep()}


@shared_task(name="courier_tracking.process_webhook")
def process_webhook_task(event_id: int) -> str:
    from .webhooks import process

    return process(event_id)


@shared_task(name="courier_tracking.reminders")
def reminders_task() -> dict:
    from .reminders import run

    return run()


@shared_task(name="courier_tracking.refresh_catalog")
def refresh_catalog_task() -> bool:
    from .services import refresh_catalog

    return refresh_catalog()
