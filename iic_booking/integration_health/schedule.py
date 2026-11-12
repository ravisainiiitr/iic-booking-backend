"""The 15-minute schedule for the health task, kept in the database beat schedule (django_celery_beat)."""

from __future__ import annotations

from .tasks import TASK_NAME

SCHEDULE_NAME = "Integration health every 15 minutes"


def ensure_schedule(minutes: int = 15) -> str:
    """Create or update the periodic task; returns 'created', 'updated' or 'unchanged'. Safe to run repeatedly."""
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    interval, _ = IntervalSchedule.objects.get_or_create(every=minutes, period=IntervalSchedule.MINUTES)
    task = PeriodicTask.objects.filter(name=SCHEDULE_NAME).first()
    if task is None:
        PeriodicTask.objects.create(name=SCHEDULE_NAME, task=TASK_NAME, interval=interval, enabled=True)
        return "created"
    if task.interval_id == interval.pk and task.enabled and task.task == TASK_NAME:
        return "unchanged"
    task.interval, task.enabled, task.task, task.crontab = interval, True, TASK_NAME, None
    task.save()
    return "updated"
