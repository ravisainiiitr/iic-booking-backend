# Add BookingBufferConfig for daily "Booking Not Utilized" check (buffer time in days)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0060_equipment_weekly_view_controls"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingBufferConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "buffer_days",
                    models.PositiveIntegerField(
                        default=2,
                        help_text="After a booked slot start time, wait this many days before auto-marking as Booking Not Utilized if sample not received/rejected/processing. Set to 0 to disable.",
                        verbose_name="Buffer time (days)",
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(
                        default=True,
                        help_text="When unchecked, the daily 20:00 check is skipped.",
                        verbose_name="Enabled",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Booking buffer config",
                "verbose_name_plural": "Booking buffer configs",
                "ordering": ["pk"],
            },
        ),
    ]
