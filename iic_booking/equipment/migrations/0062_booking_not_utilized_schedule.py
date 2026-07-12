# Schedule daily "Booking Not Utilized" check at 20:00 (Asia/Kolkata)

from django.db import migrations


def create_buffer_check_schedule(apps, schema_editor):
    """Create CrontabSchedule and PeriodicTask for check_booking_not_utilized at 20:00 daily."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    BookingBufferConfig = apps.get_model("equipment", "BookingBufferConfig")

    # 20:00 daily in project timezone
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="20",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    if not PeriodicTask.objects.filter(name="Daily booking not utilized check 20:00").exists():
        PeriodicTask.objects.create(
            name="Daily booking not utilized check 20:00",
            task="equipment.check_booking_not_utilized",
            crontab=crontab,
            enabled=True,
        )

    # Create default config if none exists
    if not BookingBufferConfig.objects.exists():
        BookingBufferConfig.objects.create(buffer_days=2, enabled=True)


def remove_buffer_check_schedule(apps, schema_editor):
    """Remove the periodic task (reverse migration)."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily booking not utilized check 20:00").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0061_bookingbufferconfig"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_buffer_check_schedule, remove_buffer_check_schedule),
    ]
