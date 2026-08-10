from django.db import migrations


def create_auto_completion_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    crontab, _ = CrontabSchedule.objects.get_or_create(
        minute="*/15",
        hour="*",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )

    name = "Auto complete bookings after end when result data exists"
    if not PeriodicTask.objects.filter(name=name).exists():
        PeriodicTask.objects.create(
            name=name,
            task="equipment.auto_complete_bookings_with_data_after_end",
            crontab=crontab,
            enabled=True,
        )


def remove_auto_completion_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name="Auto complete bookings after end when result data exists"
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0185_r9_extension_grace_minutes"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_auto_completion_schedule, remove_auto_completion_schedule),
    ]

