"""Celery tasks for Laboratory Infrastructure."""

from __future__ import annotations

import logging

from celery import shared_task

from iic_booking.equipment.peak_window import defer_during_peak

logger = logging.getLogger(__name__)


@shared_task(name="lab_infrastructure.run_health_detectors")
@defer_during_peak("lab_infrastructure.run_health_detectors")
def run_health_detectors_task() -> dict:
    from iic_booking.lab_infrastructure.services.detectors import run_health_detectors

    result = run_health_detectors()
    logger.info("lab_infrastructure.run_health_detectors: %s", result)
    return result
