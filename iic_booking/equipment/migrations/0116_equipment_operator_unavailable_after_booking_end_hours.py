from django.db import migrations, models


def create_auto_operator_unavailable_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="20",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    if not PeriodicTask.objects.filter(name="Auto operator unavailable after booking end 20:30").exists():
        PeriodicTask.objects.create(
            name="Auto operator unavailable after booking end 20:30",
            task="equipment.auto_mark_operator_unavailable_after_booking_end",
            crontab=crontab,
            enabled=True,
        )


def remove_auto_operator_unavailable_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Auto operator unavailable after booking end 20:30").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0115_equipment_results_base_location"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="operator_unavailable_after_booking_end_hours",
            field=models.PositiveIntegerField(
                default=24,
                help_text=(
                    "After the last slot end time, if the booking is still PENDING or BOOKED and sample "
                    'lifecycle has staff activity beyond "Sample Sent", automatically mark Operator Unavailable '
                    "(full refund) once this many hours have passed. Set to 0 to disable. Bookings with only "
                    '"Sample Sent" or no trace are handled by the Booking Not Utilized job instead.'
                ),
                verbose_name="Auto Operator Unavailable (hours after booking end)",
            ),
        ),
        migrations.RunPython(create_auto_operator_unavailable_schedule, remove_auto_operator_unavailable_schedule),
    ]
