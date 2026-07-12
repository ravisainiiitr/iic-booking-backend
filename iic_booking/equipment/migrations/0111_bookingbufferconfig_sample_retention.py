from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0110_booking_rating_criteria"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingbufferconfig",
            name="sample_retention_days",
            field=models.PositiveIntegerField(
                default=60,
                help_text='After the sample is marked "Completed" (Analyzed / Ready for pickup), wait this many days before auto-archiving the sample in Sample Lifecycle. Set to 0 to disable auto-archive.',
                verbose_name="Sample retention (days)",
            ),
        ),
        migrations.AddField(
            model_name="bookingbufferconfig",
            name="auto_archive_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When unchecked, the daily auto-archive task is skipped.",
                verbose_name="Auto-archive enabled",
            ),
        ),
    ]

