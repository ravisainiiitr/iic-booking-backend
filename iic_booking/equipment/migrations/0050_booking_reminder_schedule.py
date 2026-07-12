# Schedule daily booking reminders at 8:30 AM (project timezone: Asia/Kolkata)

from django.db import migrations


def create_booking_reminder_schedule(apps, schema_editor):
    """Create CrontabSchedule and PeriodicTask for send_booking_reminders at 8:30 AM daily."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    # 8:30 AM daily in project timezone
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="8",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    if not PeriodicTask.objects.filter(name="Daily booking reminders 8:30 AM").exists():
        PeriodicTask.objects.create(
            name="Daily booking reminders 8:30 AM",
            task="equipment.send_booking_reminders",
            crontab=crontab,
            enabled=True,
        )


def remove_booking_reminder_schedule(apps, schema_editor):
    """Remove the periodic task and optionally the crontab (reverse migration)."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily booking reminders 8:30 AM").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0049_ensure_booking_completed_at"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_booking_reminder_schedule, remove_booking_reminder_schedule),
    ]
