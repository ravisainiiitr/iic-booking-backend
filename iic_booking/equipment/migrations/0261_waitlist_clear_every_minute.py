from django.db import migrations
from django.utils import timezone

TASK = "Waitlist pre-reference clear (every 5 min)"
NEW_NAME = "Waitlist pre-reference clear (every minute)"


def _set_interval(apps, name_from, name_to, minutes):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTasks = apps.get_model("django_celery_beat", "PeriodicTasks")
    schedule, _ = IntervalSchedule.objects.get_or_create(every=minutes, period="minutes")
    if PeriodicTask.objects.filter(name=name_from).update(name=name_to, interval=schedule):
        # Tells the running beat scheduler to reload its schedule.
        PeriodicTasks.objects.update_or_create(ident=1, defaults={"last_update": timezone.now()})


def forwards(apps, schema_editor):
    _set_interval(apps, TASK, NEW_NAME, 1)


def backwards(apps, schema_editor):
    _set_interval(apps, NEW_NAME, TASK, 5)


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0260_booking_created_via"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
