"""Nightly IITR Connect membership sync (02:40 IST). The task returns immediately while IITR_CONNECT_ENABLED is off."""

from django.db import migrations

TASK_NAME = "IITR Connect: nightly community membership sync (02:40 IST)"


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="40", hour="2", day_of_week="*", day_of_month="*", month_of_year="*", timezone="Asia/Kolkata"
    )
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(name=TASK_NAME, task="connect.nightly_membership_sync", crontab=crontab, enabled=True)


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("connect", "0002_seed_general_categories"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(create_schedule, remove_schedule)]
