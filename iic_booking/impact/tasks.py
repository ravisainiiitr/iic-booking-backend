import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="impact.weekly_discovery")
def impact_weekly_discovery_task() -> dict:
    from .service import weekly_discovery

    result = weekly_discovery()
    logger.info("impact.weekly_discovery: %s", result)
    return result
