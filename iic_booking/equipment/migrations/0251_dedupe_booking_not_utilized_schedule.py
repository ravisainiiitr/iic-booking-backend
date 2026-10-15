# Migrations 0062 and 0142 each registered equipment.check_booking_not_utilized under a
# different name on the same 20:00 crontab, so the check ran twice per evening.
# Keep one row (the oldest enabled one) and drop the identical extras.

from django.db import migrations
from django.utils import timezone

TASK = "equipment.check_booking_not_utilized"


def _schedule_key(task):
    return (
        task.crontab_id,
        task.interval_id,
        getattr(task, "solar_id", None),
        getattr(task, "clocked_id", None),
        task.args,
        task.kwargs,
    )


def dedupe_booking_not_utilized_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTasks = apps.get_model("django_celery_beat", "PeriodicTasks")

    rows = list(PeriodicTask.objects.filter(task=TASK).order_by("pk"))
    if len(rows) < 2:
        return
    keep = next((r for r in rows if r.enabled), rows[0])
    extras = [r.pk for r in rows if r.pk != keep.pk and _schedule_key(r) == _schedule_key(keep)]
    if not extras:
        return
    PeriodicTask.objects.filter(pk__in=extras).delete()
    # Historical models do not fire django_celery_beat's change signals; bump the marker
    # so a running DatabaseScheduler reloads its schedule.
    PeriodicTasks.objects.update_or_create(ident=1, defaults={"last_update": timezone.now()})


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0250_searchsynonym"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(dedupe_booking_not_utilized_schedule, migrations.RunPython.noop),
    ]
