from django.db import migrations

TASK_NAME = "upkeep-daily-scan"


def seed(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    # 07:00 IST (01:30 UTC): maintenance due / overdue, uptime below target, repeated breakdowns, UPS health.
    cron, _ = CrontabSchedule.objects.get_or_create(minute="30", hour="1", day_of_week="*", day_of_month="*",
                                                    month_of_year="*", timezone="UTC")
    PeriodicTask.objects.get_or_create(name=TASK_NAME, defaults={"task": "upkeep.scan", "crontab": cron, "enabled": True})


def unseed(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("upkeep", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
