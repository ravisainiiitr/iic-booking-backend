from django.db import migrations, models


def create_auto_operator_absent_disruption_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="35",
        hour="20",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
        timezone="Asia/Kolkata",
    )

    name = "Auto operator absent disruption after booking end 20:35"
    if not PeriodicTask.objects.filter(name=name).exists():
        PeriodicTask.objects.create(
            name=name,
            task="equipment.auto_mark_operator_absent_disruption_after_booking_end",
            crontab=crontab,
            enabled=True,
        )


def remove_auto_operator_absent_disruption_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name="Auto operator absent disruption after booking end 20:35"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0121_booking_quota_period_anchor_at"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="operator_absent_disruption_after_booking_end_hours",
            field=models.PositiveIntegerField(
                default=48,
                help_text=(
                    "After the last slot end time, if the booking is still PENDING or BOOKED and the sample "
                    'lifecycle status remains stuck at "Sample Accepted" or "Processing" for this many hours, '
                    "treat it as Operator Absent disruption and trigger the refund/reschedule choice flow. "
                    "Set to 0 to disable."
                ),
                verbose_name="Auto Operator Absent Disruption (hours after booking end)",
            ),
        ),
        migrations.RunPython(
            create_auto_operator_absent_disruption_schedule,
            remove_auto_operator_absent_disruption_schedule,
        ),
    ]

