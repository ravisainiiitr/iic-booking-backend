from django.db import migrations


def create_sample_submission_deadline_reminder_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_10_minutes, _ = IntervalSchedule.objects.get_or_create(
        every=10,
        period="minutes",
    )

    name = "Sample submission deadline reminders (every 10 min)"
    if not PeriodicTask.objects.filter(name=name).exists():
        PeriodicTask.objects.create(
            name=name,
            task="equipment.send_sample_submission_deadline_reminders",
            interval=every_10_minutes,
            enabled=True,
        )


def remove_sample_submission_deadline_reminder_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name="Sample submission deadline reminders (every 10 min)"
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0168_booking_sample_submission_deadline_reminder"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            create_sample_submission_deadline_reminder_schedule,
            remove_sample_submission_deadline_reminder_schedule,
        ),
    ]
