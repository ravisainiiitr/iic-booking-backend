import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="impact.weekly_discovery")
def impact_weekly_discovery_task() -> dict:
    from .service import weekly_discovery

    result = weekly_discovery()
    logger.info("impact.weekly_discovery: %s", result)
    return result


@shared_task(name="impact.daily_publication_scan")
def impact_daily_publication_scan_task() -> dict:
    from .service import daily_publication_scan

    result = daily_publication_scan()
    logger.info("impact.daily_publication_scan: %s", result)
    return result


@shared_task(name="impact.author_confirmed")
def impact_author_confirmed_task(user_id: int) -> dict:
    from .author_review import follow_up

    result = follow_up(user_id)
    logger.info("impact.author_confirmed user=%s: %s", user_id, result)
    return result
