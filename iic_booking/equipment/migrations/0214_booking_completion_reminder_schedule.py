from django.db import migrations

TASK_NAME = "Booking completion reminders to OIC / Lab in-charge 09:00 IST"


def create_completion_reminder_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="9",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(
            name=TASK_NAME,
            task="equipment.send_booking_completion_reminders",
            crontab=crontab,
            enabled=True,
        )


def remove_completion_reminder_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0213_equipment_important_instruction_by_user_type"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_completion_reminder_schedule, remove_completion_reminder_schedule),
    ]
