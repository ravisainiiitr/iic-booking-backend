# Generated manually for disruption-pending workflow

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0118_urgentbookingrequest_reviewer_comment"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="disruption_kind",
            field=models.CharField(
                blank=True,
                choices=[("MAINTENANCE", "Maintenance / equipment"), ("OPERATOR_ABSENT", "Operator unavailable")],
                help_text="Set when status is Awaiting your choice (disruption); cleared when resolved.",
                max_length=32,
                null=True,
                verbose_name="Disruption kind",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="disruption_release_slot_status",
            field=models.CharField(
                blank=True,
                help_text="DailySlot status to apply when this booking is refunded or auto-cancelled under disruption policy (e.g. Under Maintenance vs Operator Absent).",
                max_length=32,
                null=True,
                verbose_name="Disruption slot release status",
            ),
        ),
        migrations.AlterField(
            model_name="booking",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("WAITLISTED", "Waitlisted"),
                    ("BOOKED", "Booked"),
                    ("DISRUPTION_PENDING", "Awaiting your choice (disruption)"),
                    ("HOLD", "Hold"),
                    ("COMPLETED", "Completed"),
                    ("CANCELLED", "Cancelled"),
                    ("ABSENT", "Operator Unavailable"),
                    ("REFUNDED", "Refunded"),
                    ("BOOKING_NOT_UTILIZED", "Booking Not Utilized"),
                ],
                default="PENDING",
                help_text="Booking status",
                max_length=30,
            ),
        ),
    ]
