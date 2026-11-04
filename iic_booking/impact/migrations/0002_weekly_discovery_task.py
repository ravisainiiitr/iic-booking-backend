from django.db import migrations

TASK_NAME = "impact-weekly-publication-discovery"


def seed(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    # Mondays 08:00 IST (02:30 UTC): look up new papers for researchers who added their ORCID iD.
    cron, _ = CrontabSchedule.objects.get_or_create(minute="30", hour="2", day_of_week="1", day_of_month="*",
                                                    month_of_year="*", timezone="UTC")
    PeriodicTask.objects.get_or_create(name=TASK_NAME, defaults={"task": "impact.weekly_discovery", "crontab": cron,
                                                                 "enabled": True})


def unseed(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("impact", "0001_initial"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
