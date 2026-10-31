"""Automatic courier tracking removed (31 Oct 2026 committee): delete its periodic schedules.

Data only. Tables and stored shipments are kept (courier details are now entered by hand and shown to the lab).
Not reversible on purpose: the schedules must not come back with a rollback of this migration alone.
"""

from django.db import migrations

RETIRED_TASKS = [
    "courier_tracking.scheduled_checks",
    "courier_tracking.reminders",
    "courier_tracking.refresh_catalog",
    "courier_tracking.sweep",
]


def delete_schedules(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(task__in=RETIRED_TASKS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("courier_tracking", "0002_templates_and_schedules"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(delete_schedules, migrations.RunPython.noop),
    ]
