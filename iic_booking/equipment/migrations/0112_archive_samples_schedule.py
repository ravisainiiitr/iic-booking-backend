from django.db import migrations


def create_sample_archive_schedule(apps, schema_editor):
    """Create CrontabSchedule and PeriodicTask for archive_expired_samples."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    BookingBufferConfig = apps.get_model("equipment", "BookingBufferConfig")

    # 19:00 daily in project timezone (Asia/Kolkata)
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="19",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    if not PeriodicTask.objects.filter(name="Daily sample auto-archive 19:00").exists():
        PeriodicTask.objects.create(
            name="Daily sample auto-archive 19:00",
            task="equipment.archive_expired_samples",
            crontab=crontab,
            enabled=True,
        )

    # Create default config if none exists (keep existing buffer defaults)
    if not BookingBufferConfig.objects.exists():
        BookingBufferConfig.objects.create(buffer_days=2, enabled=True, sample_retention_days=60, auto_archive_enabled=True)


def remove_sample_archive_schedule(apps, schema_editor):
    """Remove the periodic task (reverse migration)."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Daily sample auto-archive 19:00").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0111_bookingbufferconfig_sample_retention"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_sample_archive_schedule, remove_sample_archive_schedule),
    ]

