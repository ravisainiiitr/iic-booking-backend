from django.db import migrations, models


def forwards_copy_show_flag(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    # Preserve prior opt-in where set; otherwise leave new default True for fresh installs
    # that already ran AddField with default=True.
    for eq in Equipment.objects.all().only("pk", "show_completion_countdown"):
        if getattr(eq, "show_completion_countdown", False):
            Equipment.objects.filter(pk=eq.pk).update(show_lifecycle_countdowns=True)


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0162_equipment_completion_countdown"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="show_lifecycle_countdowns",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When enabled, booking details show live countdowns: time to submit sample (before Sample Accepted), "
                    "booking time remaining (after Sample Accepted until slot end), and time to collect sample "
                    "(after booking completed until discard deadline)."
                ),
                verbose_name="Show sample lifecycle countdowns",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="sample_submission_lead_hours",
            field=models.PositiveIntegerField(
                default=24,
                help_text=(
                    "Users must submit samples this many hours before the booked slot starts. "
                    "Atmosphere-sensitive bookings may submit up to slot start instead. "
                    "Set to 0 to use slot start as the deadline."
                ),
                verbose_name="Sample submission lead time (hours before slot start)",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="sample_collect_deadline_hours",
            field=models.PositiveIntegerField(
                default=72,
                help_text=(
                    "After the booking is completed, users have this many hours to collect the sample "
                    "before the lab may discard it. Set to 0 to hide the collect-sample countdown."
                ),
                verbose_name="Sample collect / discard deadline (hours after completion)",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="atmosphere_sensitive_sample",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When True, the sample may be submitted at slot start instead of the normal submission lead time. "
                    "Staff are notified and should not mark Booking Not Utilized before the slot begins for delayed submission."
                ),
                verbose_name="Atmosphere-sensitive sample",
            ),
        ),
        migrations.RunPython(forwards_copy_show_flag, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="equipment",
            name="show_completion_countdown",
        ),
        migrations.RemoveField(
            model_name="equipment",
            name="completion_countdown_hours",
        ),
    ]
