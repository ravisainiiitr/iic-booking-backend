"""Monthly refresh of manufacturer references (Celery Beat, 03:40 on the 1st)."""

from django.db import migrations

TASK = ("IITR Assistant: refresh manufacturer references (monthly)", "research_copilot.refresh_equipment_references")


def forwards(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="40", hour="3", day_of_week="*", day_of_month="1", month_of_year="*")
    if not PeriodicTask.objects.filter(task=TASK[1]).exists():
        PeriodicTask.objects.create(name=TASK[0], task=TASK[1], crontab=crontab, enabled=True)


def backwards(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(task=TASK[1]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("research_copilot", "0006_manufacturer_references"),
        ("django_celery_beat", "0018_improve_crontab_helptext"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
