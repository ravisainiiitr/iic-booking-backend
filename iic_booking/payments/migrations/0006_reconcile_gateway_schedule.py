from django.db import migrations

TASK_NAME = "Razorpay reconciliation (daily; idle while Razorpay is off)"


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="0", hour="1", day_of_week="*", day_of_month="*", month_of_year="*",  # 06:30 IST
    )
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(name=TASK_NAME, task="payments.reconcile_gateway", crontab=crontab, enabled=True)


def remove_schedule(apps, schema_editor):
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0005_gateway_charges_and_settlement_lines"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [migrations.RunPython(create_schedule, remove_schedule)]
