from django.db import migrations

SYNC_TASK_NAME = "I-STEM sync (every 5 minutes)"


def create_sync_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="*/5", hour="*", day_of_week="*", day_of_month="*", month_of_year="*",
    )
    if not PeriodicTask.objects.filter(name=SYNC_TASK_NAME).exists():
        # Created disabled; switched on with automatic FBR (Apply I-STEM Pilot Config, booking_steps=automatic).
        PeriodicTask.objects.create(name=SYNC_TASK_NAME, task="istem.run_sync", crontab=crontab, enabled=False)


def remove_sync_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=SYNC_TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("istem", "0007_portal_cancellation"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_sync_schedule, remove_sync_schedule),
    ]
