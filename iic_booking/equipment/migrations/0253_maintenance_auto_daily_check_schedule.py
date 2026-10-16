# Registers equipment.maintenance_auto_daily_check at 08:30 Asia/Kolkata. Keyed on the task (not the row name) so it
# never adds a second row for the same task (see 0251); identical extras are removed.

from django.db import migrations
from django.utils import timezone

TASK = "equipment.maintenance_auto_daily_check"
NAME = "Equipment under maintenance: daily check 08:30 IST"


def _bump(apps):
    PeriodicTasks = apps.get_model("django_celery_beat", "PeriodicTasks")
    PeriodicTasks.objects.update_or_create(ident=1, defaults={"last_update": timezone.now()})


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab = (
        CrontabSchedule.objects.filter(
            minute="30", hour="8", day_of_week="*", day_of_month="*", month_of_year="*", timezone="Asia/Kolkata"
        )
        .order_by("pk")
        .first()
    )
    if crontab is None:
        crontab = CrontabSchedule.objects.create(
            minute="30", hour="8", day_of_week="*", day_of_month="*", month_of_year="*", timezone="Asia/Kolkata"
        )
    rows = list(PeriodicTask.objects.filter(task=TASK).order_by("pk"))
    if not rows:
        if PeriodicTask.objects.filter(name=NAME).exists():
            return
        PeriodicTask.objects.create(name=NAME, task=TASK, crontab=crontab, enabled=True)
    else:
        keep = next((r for r in rows if r.enabled), rows[0])
        PeriodicTask.objects.filter(task=TASK).exclude(pk=keep.pk).delete()
    _bump(apps)


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(task=TASK).delete()
    _bump(apps)


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0252_equipment_maintenance_auto"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
