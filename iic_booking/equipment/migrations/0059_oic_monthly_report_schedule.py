# Schedule OIC monthly equipment report (1st of month, 00:05 – report for previous month)

from django.db import migrations

from iic_booking.compat.celery_crontab import crontab_get_or_create


def create_oic_report_schedule(apps, schema_editor):
    """Create CrontabSchedule and PeriodicTask for send_oic_monthly_reports on 1st of each month at 00:05."""
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    crontab, _ = crontab_get_or_create(
        CrontabSchedule,
        minute="5",
        hour="0",
        day_of_week="*",
        day_of_month="1",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    if not PeriodicTask.objects.filter(name="OIC monthly equipment report (1st of month)").exists():
        PeriodicTask.objects.create(
            name="OIC monthly equipment report (1st of month)",
            task="equipment.send_oic_monthly_reports",
            crontab=crontab,
            enabled=True,
        )


def remove_oic_report_schedule(apps, schema_editor):
    """Remove the periodic task (reverse migration)."""
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="OIC monthly equipment report (1st of month)").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0058_booking_charge_recalculation_pending_amount"),
        ("django_celery_beat", "0016_alter_crontabschedule_timezone"),
    ]

    operations = [
        migrations.RunPython(create_oic_report_schedule, remove_oic_report_schedule),
    ]
