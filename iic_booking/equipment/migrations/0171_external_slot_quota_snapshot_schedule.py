from django.db import migrations


def create_external_slot_quota_snapshot_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_5_minutes, _ = IntervalSchedule.objects.get_or_create(
        every=5,
        period="minutes",
    )

    name = "External slot quota snapshots (every 5 min)"
    if not PeriodicTask.objects.filter(name=name).exists():
        PeriodicTask.objects.create(
            name=name,
            task="equipment.generate_external_slot_quota_snapshots",
            interval=every_5_minutes,
            enabled=True,
        )


def remove_external_slot_quota_snapshot_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(
        name="External slot quota snapshots (every 5 min)"
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0170_external_weekly_slot_quota"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            create_external_slot_quota_snapshot_schedule,
            remove_external_slot_quota_snapshot_schedule,
        ),
    ]
