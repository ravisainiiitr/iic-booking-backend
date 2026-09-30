from django.db import migrations

TASK_NAME = "Next week slot generation Wed 20:30 IST"


def create_next_week_slots_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    # Celery crontab day_of_week: 0 = Sunday, so 3 = Wednesday.
    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="30",
        hour="20",
        day_of_week="3",
        day_of_month="*",
        month_of_year="*",
    )
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(
            name=TASK_NAME,
            task="equipment.prepare_next_week_slots",
            crontab=crontab,
            enabled=True,
        )


def remove_next_week_slots_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0210_lab_operator_help_text"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_next_week_slots_schedule, remove_next_week_slots_schedule),
    ]
