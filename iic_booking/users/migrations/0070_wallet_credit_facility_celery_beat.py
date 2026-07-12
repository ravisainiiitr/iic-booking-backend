# Schedule hourly expiry check for wallet recharge credit windows (django-celery-beat)

from django.db import migrations


def create_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="5",
        hour="*",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    if not PeriodicTask.objects.filter(name="Hourly wallet credit facility expiry").exists():
        PeriodicTask.objects.create(
            name="Hourly wallet credit facility expiry",
            task="users.expire_wallet_credit_facilities",
            crontab=crontab,
            enabled=True,
        )


def remove_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Hourly wallet credit facility expiry").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0069_wallet_credit_facility"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_schedule, remove_schedule),
    ]
