from django.db import migrations

from iic_booking.compat.celery_crontab import crontab_get_or_create


def create_booking_not_utilized_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    crontab, _ = crontab_get_or_create(
        CrontabSchedule,
        minute="0",
        hour="20",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    name = "Auto booking not utilized 20:00 IST (weekdays per task logic)"
    if not PeriodicTask.objects.filter(name=name).exists():
        PeriodicTask.objects.create(
            name=name,
            task="equipment.check_booking_not_utilized",
            crontab=crontab,
            enabled=True,
        )


def remove_booking_not_utilized_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name="Auto booking not utilized 20:00 IST (weekdays per task logic)"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0141_equipment_operator_coverage"),
        ("django_celery_beat", "0016_alter_crontabschedule_timezone"),
    ]

    operations = [
        migrations.RunPython(create_booking_not_utilized_schedule, remove_booking_not_utilized_schedule),
    ]
