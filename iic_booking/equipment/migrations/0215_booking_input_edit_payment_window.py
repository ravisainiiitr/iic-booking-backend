from django.db import migrations, models

TASK_NAME = "Revert unpaid booking input edits (every minute)"


def create_expire_unpaid_input_edits_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    every_minute, _ = IntervalSchedule.objects.get_or_create(every=1, period="minutes")
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(
            name=TASK_NAME,
            task="equipment.expire_unpaid_input_edits",
            interval=every_minute,
            enabled=True,
        )


def remove_expire_unpaid_input_edits_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0214_booking_completion_reminder_schedule"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="charge_recalculation_pay_deadline",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text="When the booking user's own input edit raised the charge: the extra amount must be paid before this time, otherwise the edit is reverted.",
                null=True,
                verbose_name="Pay deadline for an input edit",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="charge_recalculation_revert_snapshot",
            field=models.JSONField(
                blank=True,
                help_text="Input values and charge before an unpaid input edit; restored if the extra amount is not paid in time.",
                null=True,
                verbose_name="Pre-edit inputs and charge",
            ),
        ),
        migrations.RunPython(
            create_expire_unpaid_input_edits_schedule, remove_expire_unpaid_input_edits_schedule
        ),
    ]
