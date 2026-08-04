"""Post-migrate seed for Lab Infrastructure periodic tasks."""

from __future__ import annotations

import logging

from django.db.models.signals import post_migrate
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_migrate)
def ensure_lab_infrastructure_periodic_tasks(sender, **kwargs):
    if sender.name != "iic_booking.lab_infrastructure":
        return
    try:
        from django_celery_beat.models import IntervalSchedule, PeriodicTask

        interval_5m, _ = IntervalSchedule.objects.get_or_create(every=5, period=IntervalSchedule.MINUTES)
        PeriodicTask.objects.update_or_create(
            name="Lab Infrastructure Health Detectors",
            defaults={
                "task": "lab_infrastructure.run_health_detectors",
                "interval": interval_5m,
                "crontab": None,
                "enabled": True,
            },
        )
    except Exception:
        logger.exception("Failed to seed lab infrastructure periodic tasks")
