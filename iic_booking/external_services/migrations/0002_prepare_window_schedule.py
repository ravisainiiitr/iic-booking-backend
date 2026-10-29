from django.db import migrations

TASK_NAME = "External booking window: prepare opened weeks (every 15 min)"


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="*/15",
        hour="*",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    # The task does nothing while the external booking window is disabled (the default), so it can be enabled.
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(
            name=TASK_NAME,
            task="external_services.prepare_window",
            crontab=crontab,
            enabled=True,
        )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("external_services", "0001_external_booking_window"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(create_schedule, remove_schedule)]
