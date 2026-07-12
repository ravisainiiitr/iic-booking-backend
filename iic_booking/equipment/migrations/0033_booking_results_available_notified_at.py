# Generated migration: add results_available_notified_at to Booking

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0032_booking_virtual_booking_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="results_available_notified_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the user was notified (in-app and email) that result files are available in S3",
                null=True,
                verbose_name="Results available notified at",
            ),
        ),
    ]
