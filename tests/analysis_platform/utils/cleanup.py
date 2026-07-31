"""Cleanup helpers for non-transactional / scripted runs."""

from __future__ import annotations

import logging

from django.db import transaction

from tests.analysis_platform.seeder import AnalysisPlatformSeeder, SeedResult, SEED_PREFIX

logger = logging.getLogger(__name__)


def cleanup_seed(seed: SeedResult) -> None:
    AnalysisPlatformSeeder().cleanup(seed)


@transaction.atomic
def cleanup_apt_prefix(*, limit: int = 200) -> int:
    """
    Remove leftover APT-prefixed equipment/bookings from aborted script runs.
    Safe for local/CI harness DBs only — do not point at production.
    """
    from iic_booking.equipment.models import Booking, Equipment
    from iic_booking.remote_analysis.models import AnalysisWorkstation
    from iic_booking.remote_analysis.workflow_models import AnalysisJob, AnalysisWorkflow

    removed = 0
    for booking in Booking.objects.filter(virtual_booking_id__startswith=SEED_PREFIX)[:limit]:
        AnalysisJob.objects.filter(booking=booking).delete()
        booking.delete()
        removed += 1
    for eq in Equipment.objects.filter(code__startswith=SEED_PREFIX)[:limit]:
        eq.delete()
        removed += 1
    for ws in AnalysisWorkstation.objects.filter(agent_id__startswith="apt-")[:limit]:
        ws.delete()
        removed += 1
    for wf in AnalysisWorkflow.objects.filter(slug__startswith="pxrd-")[:limit]:
        if "test" in (wf.description or "").lower() or "Harness" in (wf.description or ""):
            wf.delete()
            removed += 1
    logger.info("APT cleanup removed ~%s objects", removed)
    return removed
