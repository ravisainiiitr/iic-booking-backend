"""Celery tasks for Laboratory Infrastructure."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="lab_infrastructure.run_health_detectors")
def run_health_detectors_task() -> dict:
    from iic_booking.lab_infrastructure.services.detectors import run_health_detectors

    result = run_health_detectors()
    logger.info("lab_infrastructure.run_health_detectors: %s", result)
    return result
