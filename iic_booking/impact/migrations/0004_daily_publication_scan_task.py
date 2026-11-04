from django.db import migrations

TASK_NAME = "impact-daily-publication-scan"


def seed(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    # 03:15 IST (21:45 UTC) daily. The task does nothing until PUBLICATION_MAPPING_ENABLED is switched on.
    cron, _ = CrontabSchedule.objects.get_or_create(minute="45", hour="21", day_of_week="*", day_of_month="*",
                                                    month_of_year="*", timezone="UTC")
    PeriodicTask.objects.get_or_create(name=TASK_NAME, defaults={"task": "impact.daily_publication_scan",
                                                                 "crontab": cron, "enabled": True})


def unseed(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("impact", "0003_publication_facility_mapping"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
