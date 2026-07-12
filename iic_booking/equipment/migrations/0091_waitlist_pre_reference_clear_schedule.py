from django.db import migrations


def create_waitlist_pre_reference_clear_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    every_5_minutes, _ = IntervalSchedule.objects.get_or_create(
        every=5,
        period="minutes",
    )

    if not PeriodicTask.objects.filter(name="Waitlist pre-reference clear (every 5 min)").exists():
        PeriodicTask.objects.create(
            name="Waitlist pre-reference clear (every 5 min)",
            task="equipment.clear_waitlist_before_reference",
            interval=every_5_minutes,
            enabled=True,
        )


def remove_waitlist_pre_reference_clear_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="Waitlist pre-reference clear (every 5 min)").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0090_alter_dynamicinputfield_field_type_length"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            create_waitlist_pre_reference_clear_schedule,
            remove_waitlist_pre_reference_clear_schedule,
        ),
    ]

