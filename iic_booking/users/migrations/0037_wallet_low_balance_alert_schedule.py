# Schedule daily wallet low balance alerts at 11:00 AM (Asia/Kolkata)

from django.db import migrations


def create_wallet_low_balance_schedule(apps, schema_editor):
    """Create CrontabSchedule and PeriodicTask for send_wallet_low_balance_alerts at 11:00 AM daily."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="11",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    if not PeriodicTask.objects.filter(name="Daily wallet low balance alerts 11:00 AM").exists():
        PeriodicTask.objects.create(
            name="Daily wallet low balance alerts 11:00 AM",
            task="users.send_wallet_low_balance_alerts",
            crontab=crontab,
            enabled=True,
        )


def remove_wallet_low_balance_schedule(apps, schema_editor):
    """Remove the periodic task (reverse migration)."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily wallet low balance alerts 11:00 AM").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0036_user_wallet_low_balance_alert"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_wallet_low_balance_schedule, remove_wallet_low_balance_schedule),
    ]
