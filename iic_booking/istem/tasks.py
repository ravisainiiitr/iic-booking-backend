"""Scheduled I-STEM sync (2 Nov 2026). Runs only while automatic FBR is on (``ISTEM_AUTOMATIC_FBR``); it is scheduled
every few minutes from the database beat schedule when the integration goes live. Bookings made before
``ISTEM_SYNC_START_AT`` are never sent."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="istem.run_sync", ignore_result=True)
def run_istem_sync(limit: int = 50) -> dict:
    """Queue Create Booking for newly paid bookings, FBR Execute for completed ones and the closing call for cancelled
    ones, then process due jobs."""
    from . import conf
    from .service import enqueue_cancellations, enqueue_eligible, enqueue_eligible_executions, process_due_jobs

    if not conf.automatic_fbr_enabled() or conf.missing_live_settings():
        return {"skipped": True}
    queued = enqueue_eligible(limit=limit)
    executions = enqueue_eligible_executions(limit=limit)
    cancellations = enqueue_cancellations(limit=limit)
    processed = 0
    for _ in range(3):  # a job can queue the next one (Confirm Booking follows Create Booking)
        results = process_due_jobs(limit=limit)
        processed += len(results)
        if not results:
            break
    summary = {"queued": queued, "queued_executions": executions, "queued_cancellations": cancellations, "processed": processed}
    logger.info("istem sync: %s", summary)
    return summary
