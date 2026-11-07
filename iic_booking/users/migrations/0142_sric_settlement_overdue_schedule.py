from django.db import migrations

TASK_NAME = "SRIC settlement overdue alert (daily)"


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="30", hour="4", day_of_week="*", day_of_month="*", month_of_year="*",  # 10:00 IST
    )
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(name=TASK_NAME, task="users.alert_sric_settlement_overdue", crontab=crontab, enabled=True)


def remove_schedule(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0141_sric_ledger_status_and_sbiepay_removal"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(create_schedule, remove_schedule)]
