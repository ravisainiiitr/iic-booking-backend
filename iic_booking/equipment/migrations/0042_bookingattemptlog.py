# Booking attempt log (comprehensive success/failure log)

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0041_noslotallocationlog_urgentbookingrequest"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingAttemptLog",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                (
                    "outcome",
                    models.CharField(
                        choices=[("SUCCESS", "Success"), ("FAILED", "Failed")],
                        help_text="Whether the booking succeeded or failed",
                        max_length=20,
                    ),
                ),
                (
                    "failure_reason",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Reason for failure when outcome is FAILED",
                    ),
                ),
                ("number_of_samples", models.PositiveIntegerField(default=1)),
                ("slots_requested", models.PositiveIntegerField(default=1)),
                ("duration_minutes", models.PositiveIntegerField(blank=True, null=True)),
                (
                    "booking_id",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Booking ID when outcome is SUCCESS",
                        null=True,
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        help_text="Equipment for which booking was attempted",
                        on_delete=models.deletion.CASCADE,
                        related_name="booking_attempt_logs",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        help_text="User who attempted the booking",
                        on_delete=models.deletion.CASCADE,
                        related_name="booking_attempt_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-requested_at"],
                "verbose_name": "Booking attempt log entry",
                "verbose_name_plural": "Booking attempt log",
            },
        ),
    ]
