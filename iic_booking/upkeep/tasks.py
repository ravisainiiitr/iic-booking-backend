import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="upkeep.scan")
def upkeep_scan_task() -> dict:
    from .service import scan

    result = scan()
    logger.info("upkeep.scan: %s", result)
    return result
