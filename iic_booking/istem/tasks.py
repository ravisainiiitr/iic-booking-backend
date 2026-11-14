"""Scheduled I-STEM sync (2 Nov 2026). Runs only while automatic FBR is on (``ISTEM_AUTOMATIC_FBR``); it is scheduled
every few minutes from the database beat schedule when the integration goes live. Bookings made before
``ISTEM_SYNC_START_AT`` are never sent. Nothing is sent during the peak booking window (realtime.peak_ends_at)."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="istem.run_sync", ignore_result=True)
def run_istem_sync(limit: int = 50) -> dict:
    """Queue Create Booking for newly paid bookings, FBR Execute for completed ones and the closing call for cancelled
    ones, then process due jobs."""
    from . import conf
    from .service import (
        enqueue_cancellations, enqueue_eligible, enqueue_eligible_executions, enqueue_moves,
        enqueue_superseded_cancellations, process_due_jobs,
    )

    if not conf.automatic_fbr_enabled() or conf.missing_live_settings():
        return {"skipped": True}
    from .realtime import _schedule_after_peak, peak_ends_at

    ends_at = peak_ends_at()
    if ends_at is not None:
        # Peak booking window: nothing goes to I-STEM until it ends; one run is due then.
        _schedule_after_peak(ends_at, "scheduled sync during the peak window")
        logger.info("istem sync: deferred until the peak window ends (%s)", ends_at.isoformat())
        return {"deferred_until": ends_at.isoformat()}
    queued = enqueue_eligible(limit=limit)
    executions = enqueue_eligible_executions(limit=limit)
    cancellations = enqueue_cancellations(limit=limit)
    moves = enqueue_moves(limit=limit)
    replaced = enqueue_superseded_cancellations(limit=limit)
    processed = 0
    for _ in range(3):  # a job can queue the next one (Confirm Booking follows Create Booking)
        results = process_due_jobs(limit=limit)
        processed += len(results)
        if not results:
            break
    summary = {"queued": queued, "queued_executions": executions, "queued_cancellations": cancellations,
               "queued_moves": moves, "queued_replaced_cancellations": replaced, "processed": processed}
    logger.info("istem sync: %s", summary)
    return summary
